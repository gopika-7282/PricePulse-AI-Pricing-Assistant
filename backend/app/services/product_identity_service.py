"""
product_identity_service.py
===========================
Product Identity Resolution Service powered by PostgreSQL + Qwen3 (Ollama).

Flow:
1. Input product -> Retrieve candidates from PostgreSQL (<= 5)
   - PostgreSQL candidate retrieval ranks name/details first, with category and brand
     as low-weight supporting signals
2. Candidates -> Qwen3 (via llm_service.generate_structured)
   - Qwen3 receives ONLY input and candidate data (never touches DB directly)
   - Identity rules: name + description primary, brand ignored,
     different product type or key ingredient => different product,
     regional/multilingual names supported
3. Python validation (Pydantic)
   - Allowed decisions: MATCH_EXISTING, UNCERTAIN_MATCH, NEW_PRODUCT
   - matched_catalog_id MUST be one of candidate IDs
   - confidence in [0.0, 1.0]
   - Model/service failures raise IdentityServiceUnavailable, never an identity decision
4. Catalog Lifecycle Resolution
   - MATCH_EXISTING -> reuse row (fresh -> reuse, stale -> re-scrape)
   - MATCH -> reuse the selected catalog row
   - NOT_MATCH / UNCERTAIN_MATCH -> LangGraph creates a new catalog row
"""

from enum import Enum
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models.product_catalog import ProductCatalog
from app.services.llm_service import generate_structured
from app.config import OLLAMA_IDENTITY_TIMEOUT

logger = logging.getLogger(__name__)

STOP_WORDS: Set[str] = {
    "and", "for", "the", "with", "all", "our", "pure", "best", "new",
    "pack", "set", "combo", "gm", "ml", "kg", "ltr", "oz", "gram", "grams",
    "natural", "organic", "herbal", "original", "item", "product"
}


class IdentityDecision(str, Enum):
    MATCH = "MATCH"
    MATCH_EXISTING = "MATCH"  # compatibility alias for existing service callers
    UNCERTAIN_MATCH = "UNCERTAIN_MATCH"
    NOT_MATCH = "NOT_MATCH"
    NEW_PRODUCT = "NOT_MATCH"  # compatibility alias; public decision is NOT_MATCH


class IdentityOutput(BaseModel):
    decision: IdentityDecision = IdentityDecision.UNCERTAIN_MATCH
    matched_catalog_id: Optional[int] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    identity_factors: List[str] = Field(default_factory=list)


class IdentityServiceUnavailable(RuntimeError):
    """The identity model failed to run or returned an unusable response."""


def extract_search_tokens(text: str) -> List[str]:
    """Extract alphanumeric tokens (len >= 3) excluding generic stop words."""
    if not text:
        return []
    raw_tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return [t for t in raw_tokens if len(t) >= 3 and t not in STOP_WORDS]


def retrieve_postgres_candidates(
    db: Session,
    product_name: str,
    product_details: str = "",
    category: str = "",
    brand: str = "",
    limit: int = 5,
    exclude_catalog_ids: Optional[Set[int]] = None,
) -> List[ProductCatalog]:
    """
    Deterministic candidate retrieval from PostgreSQL (<= 5).
    Ranks token overlap on name and details first; category and brand only add
    small retrieval boosts and never determine the identity decision.
    """
    name_tokens = extract_search_tokens(product_name)
    details_tokens = extract_search_tokens(product_details)
    brand_tokens = extract_search_tokens(brand)
    all_tokens = list(dict.fromkeys(name_tokens + details_tokens + brand_tokens))

    logger.info(
        f"[CANDIDATE_SEARCH_STARTED] name='{product_name}' tokens={name_tokens[:5]}"
    )

    query = db.query(ProductCatalog)
    if exclude_catalog_ids:
        query = query.filter(~ProductCatalog.id.in_(exclude_catalog_ids))

    if not all_tokens:
        # Fallback to recent products if no tokens found
        candidates = query.order_by(ProductCatalog.id.desc()).limit(limit).all()
        return candidates

    # Build ILIKE filters on name and details
    ilike_clauses = []
    for tok in all_tokens[:8]:
        ilike_clauses.append(ProductCatalog.name.ilike(f"%{tok}%"))
        ilike_clauses.append(ProductCatalog.product_details.ilike(f"%{tok}%"))
        ilike_clauses.append(ProductCatalog.brand.ilike(f"%{tok}%"))

    if category:
        cat_tokens = extract_search_tokens(category)
        for cat_tok in cat_tokens:
            ilike_clauses.append(ProductCatalog.category.ilike(f"%{cat_tok}%"))

    matching_rows = query.filter(or_(*ilike_clauses)).all()

    # If few or no rows matched directly (e.g. multilingual / transliteration),
    # fetch all rows if table is small (<= 50) so LLM can reason over them
    if len(matching_rows) < limit:
        all_rows = query.order_by(ProductCatalog.id.desc()).limit(50).all()
        row_map = {r.id: r for r in (matching_rows + all_rows)}
        matching_rows = list(row_map.values())

    # Deterministic scoring:
    def score_candidate(cand: ProductCatalog) -> float:
        candidate_name_tokens = set(extract_search_tokens(cand.name or ""))
        candidate_detail_tokens = set(extract_search_tokens(cand.product_details or ""))
        target_name_tokens = set(name_tokens)
        name_overlap = target_name_tokens & candidate_name_tokens
        coverage = len(name_overlap) / max(1, len(target_name_tokens))
        union = target_name_tokens | candidate_name_tokens
        name_similarity = len(name_overlap) / max(1, len(union))
        detail_overlap = target_name_tokens & candidate_detail_tokens
        incoming_detail_overlap = set(details_tokens) & (candidate_name_tokens | candidate_detail_tokens)
        score = (coverage * 45.0) + (name_similarity * 30.0)
        score += (len(detail_overlap) / max(1, len(target_name_tokens))) * 15.0
        score += (len(incoming_detail_overlap) / max(1, len(details_tokens))) * 8.0
        if category and cand.category and category.casefold().strip() == cand.category.casefold().strip():
            score += 5.0
        # Brand can help retrieve a candidate, but its small weight cannot
        # outrank product identity, product type, or identifying details.
        candidate_brand_tokens = set(extract_search_tokens(cand.brand or ""))
        score += (len(set(brand_tokens) & candidate_brand_tokens) / max(1, len(set(brand_tokens)))) * 2.0
        return score

    ranked = sorted(matching_rows, key=score_candidate, reverse=True)
    top_candidates = ranked[:limit]

    logger.info(
        f"[CANDIDATES_RETRIEVED] count={len(top_candidates)} "
        f"ids={[c.id for c in top_candidates]}"
    )
    return top_candidates


def build_identity_prompt(
    product_name: str,
    product_details: str,
    category: str,
    candidates: List[Dict[str, Any]],
    brand: str = "",
    cost_price: Optional[float] = None,
    stock_quantity: Optional[int] = None,
    minimum_profit_margin: Optional[float] = None,
) -> str:
    """Build structured LLM prompt enforcing identity resolution rules."""
    candidates_repr = [
        {
            "catalog_id": c["id"],
            "product_name": c["name"],
            "category": c.get("category") or "",
            "description": c.get("product_details") or "",
            "brand": c.get("brand") or "",
        }
        for c in candidates
    ]

    return f"""INCOMING PRODUCT:
- Name: "{product_name}"
- Brand: "{brand}"
- Category: "{category}"
- Details: "{product_details}"
- Cost price: {cost_price}
- Stock quantity: {stock_quantity}
- Minimum profit margin: {minimum_profit_margin}

EXISTING CATALOG CANDIDATES (Choose at most one matching catalog_id):
{json.dumps(candidates_repr, indent=2)}

TASK:
Determine if the INCOMING PRODUCT is the EXACT SAME generic product as one of the candidates, an UNCERTAIN_MATCH, or NOT_MATCH.

CORE RULES:
1. Primary signals: product identity in the name; verify product type, intended use, ingredients/material, and details. Cost, stock, and margin are context only and MUST NOT affect identity.
2. Brand is supporting metadata only. Different brands do not make otherwise identical generic products different.
3. Same category != same product: differing product types or key ingredients mean NOT_MATCH.
4. Regional, multilingual, typo, and transliteration matches must be inferred from the complete product evidence, not a hardcoded alias list.
5. Generic reasoning only: No hardcoded brand or category bias.

REQUIRED OUTPUT FORMAT (JSON only):
{{
  "decision": "MATCH" | "UNCERTAIN_MATCH" | "NOT_MATCH",
  "matched_catalog_id": <catalog_id integer if MATCH, else null>,
  "confidence": <float 0.0 to 1.0>,
  "reason": "<concise explanation>",
  "identity_factors": ["<factor1>", "<factor2>"]
}}"""


def evaluate_product_identity(
    db: Session,
    product_name: str,
    product_details: str = "",
    category: str = "",
    brand: str = "",
    exclude_catalog_ids: Optional[Set[int]] = None,
    cost_price: Optional[float] = None,
    stock_quantity: Optional[int] = None,
    minimum_profit_margin: Optional[float] = None,
) -> IdentityOutput:
    """
    Main product identity pipeline:
    PostgreSQL candidates (<=5) -> Qwen3 (via llm_service) -> Pydantic validation.
    """
    candidates = retrieve_postgres_candidates(
        db=db,
        product_name=product_name,
        product_details=product_details,
        category=category,
        brand=brand,
        limit=5,
        exclude_catalog_ids=exclude_catalog_ids,
    )

    if not candidates:
        logger.info(f"[IDENTITY_NEW_PRODUCT] No candidates in catalog for '{product_name}'")
        return IdentityOutput(
            decision=IdentityDecision.NEW_PRODUCT,
            matched_catalog_id=None,
            confidence=1.0,
            reason="Catalog contains no candidates",
            identity_factors=["empty_catalog"],
        )

    candidate_ids = {c.id for c in candidates}
    cand_dicts = [
        {
            "id": c.id,
            "name": c.name,
            "category": c.category or "",
            "product_details": c.product_details or "",
            "brand": c.brand or "",
        }
        for c in candidates
    ]

    prompt = build_identity_prompt(
        product_name=product_name,
        product_details=product_details,
        category=category,
        brand=brand,
        candidates=cand_dicts,
        cost_price=cost_price,
        stock_quantity=stock_quantity,
        minimum_profit_margin=minimum_profit_margin,
    )

    llm_resp = generate_structured(prompt=prompt, timeout=OLLAMA_IDENTITY_TIMEOUT, operation="product_identity")

    if not llm_resp.get("success") or not llm_resp.get("data"):
        err = llm_resp.get("error") or "Qwen identity generation failed"
        logger.error("[IDENTITY_SERVICE_UNAVAILABLE] operation=product_identity error=%s", err)
        raise IdentityServiceUnavailable(err)

    data = llm_resp["data"]

    # ── Python Validation (Pydantic + candidate containment) ───────────────
    try:
        raw_decision = str(data.get("decision", "")).strip().upper()
        raw_decision = {"MATCH_EXISTING": "MATCH", "NEW_PRODUCT": "NOT_MATCH"}.get(raw_decision, raw_decision)
        if raw_decision not in {"MATCH", "UNCERTAIN_MATCH", "NOT_MATCH"}:
            raise IdentityServiceUnavailable("Qwen returned an invalid identity decision")

        raw_id = data.get("matched_catalog_id")
        raw_conf = data.get("confidence", 0.0)

        # Validate numeric confidence in [0.0, 1.0]
        try:
            conf = float(raw_conf)
            conf = max(0.0, min(1.0, conf))
        except (ValueError, TypeError):
            raise IdentityServiceUnavailable("Qwen returned an invalid identity confidence")

        validated_id = None
        if raw_decision == IdentityDecision.MATCH.value:
            if raw_id is not None:
                try:
                    int_id = int(raw_id)
                    if int_id in candidate_ids:
                        validated_id = int_id
                    else:
                        raise IdentityServiceUnavailable("Qwen selected a catalog candidate that was not supplied")
                except (ValueError, TypeError):
                    raise IdentityServiceUnavailable("Qwen returned an invalid catalog candidate ID")
            else:
                raise IdentityServiceUnavailable("Qwen returned MATCH without a catalog candidate ID")

        output = IdentityOutput(
            decision=IdentityDecision(raw_decision),
            matched_catalog_id=validated_id,
            confidence=conf,
            reason=str(data.get("reason", "")),
            identity_factors=list(data.get("identity_factors", [])),
        )
        logger.info(
            f"[IDENTITY_RESOLVED] product='{product_name}' decision={output.decision.value} "
            f"matched_id={output.matched_catalog_id} confidence={output.confidence:.2f}"
        )
        return output

    except IdentityServiceUnavailable:
        raise
    except Exception as val_exc:
        logger.error("[IDENTITY_VALIDATION_ERROR] %s", val_exc, exc_info=True)
        raise IdentityServiceUnavailable("Qwen identity response could not be validated") from val_exc


def determine_product_lifecycle(
    db: Session,
    product_name: str,
    category: str = "",
    brand: str = "",
    product_details: str = "",
) -> Dict[str, Any]:
    """Compatibility adapter for identity only; freshness belongs to LangGraph."""
    identity = evaluate_product_identity(
        db=db,
        product_name=product_name,
        product_details=product_details,
        category=category,
        brand=brand,
    )

    if identity.decision == IdentityDecision.MATCH and identity.matched_catalog_id:
        return {"decision": "MATCH", "catalog_product_id": identity.matched_catalog_id,
                "identity_decision": identity.decision.value, "match_confidence": identity.confidence,
                "reason": identity.reason}
    return {"decision": "CREATE_NEW", "catalog_product_id": None,
            "identity_decision": identity.decision.value, "match_confidence": identity.confidence,
            "reason": identity.reason}
