"""
product_identity_service.py
===========================
Product Identity Resolution Service powered by PostgreSQL + Qwen3 (Ollama).

Flow:
1. Input product -> Retrieve candidates from PostgreSQL (<= 5)
   - Deterministic SQL based on token/ILIKE overlap on product_name + details
   - Category used as supporting context only
   - Brand NEVER filtered
2. Candidates -> Qwen3 (via llm_service.generate_structured)
   - Qwen3 receives ONLY input and candidate data (never touches DB directly)
   - Identity rules: name + description primary, brand ignored,
     different product type or key ingredient => different product,
     regional/multilingual names supported
3. Python validation (Pydantic)
   - Allowed decisions: MATCH_EXISTING, UNCERTAIN_MATCH, NEW_PRODUCT
   - matched_catalog_id MUST be one of candidate IDs
   - confidence in [0.0, 1.0]
   - Fallback to safe UNCERTAIN_MATCH on any invalid data or LLM error
4. Catalog Lifecycle Resolution
   - MATCH_EXISTING -> reuse row (fresh -> reuse, stale -> re-scrape)
   - UNCERTAIN_MATCH -> create independent row marked uncertain (never silently merge)
   - NEW_PRODUCT -> create new ProductCatalog row
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
from app.services.competitor_service import has_fresh_competitor_data
from app.config import SCRAPE_FRESHNESS_THRESHOLD_DAYS

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
    limit: int = 5,
) -> List[ProductCatalog]:
    """
    Deterministic candidate retrieval from PostgreSQL (<= 5).
    Evaluates token overlap on product_name and details.
    Category is supporting context. Brand is NEVER filtered.
    """
    name_tokens = extract_search_tokens(product_name)
    details_tokens = extract_search_tokens(product_details)
    all_tokens = list(dict.fromkeys(name_tokens + details_tokens))

    logger.info(
        f"[CANDIDATE_SEARCH_STARTED] name='{product_name}' tokens={name_tokens[:5]}"
    )

    query = db.query(ProductCatalog)

    if not all_tokens:
        # Fallback to recent products if no tokens found
        candidates = query.order_by(ProductCatalog.id.desc()).limit(limit).all()
        return candidates

    # Build ILIKE filters on name and details
    ilike_clauses = []
    for tok in all_tokens[:8]:
        ilike_clauses.append(ProductCatalog.name.ilike(f"%{tok}%"))
        ilike_clauses.append(ProductCatalog.product_details.ilike(f"%{tok}%"))

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
    clean_target = product_name.lower().strip()

    def score_candidate(cand: ProductCatalog) -> float:
        cand_name = (cand.name or "").lower().strip()
        cand_details = (cand.product_details or "").lower().strip()
        score = 0.0

        if cand_name == clean_target:
            score += 100.0

        for tok in name_tokens:
            if tok in cand_name:
                score += 15.0
            if tok in cand_details:
                score += 5.0

        for tok in details_tokens:
            if tok in cand_name:
                score += 5.0
            if tok in cand_details:
                score += 2.0

        if category and cand.category and category.lower() == cand.category.lower():
            score += 3.0

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
) -> str:
    """Build structured LLM prompt enforcing identity resolution rules."""
    candidates_repr = [
        {
            "catalog_id": c["id"],
            "product_name": c["name"],
            "category": c.get("category") or "",
            "description": c.get("product_details") or "",
        }
        for c in candidates
    ]

    return f"""INCOMING PRODUCT:
- Name: "{product_name}"
- Category: "{category}"
- Details: "{product_details}"

EXISTING CATALOG CANDIDATES (Choose at most one matching catalog_id):
{json.dumps(candidates_repr, indent=2)}

TASK:
Determine if the INCOMING PRODUCT is the EXACT SAME generic product as one of the candidates, an UNCERTAIN_MATCH, or NOT_MATCH.

CORE RULES:
1. Primary signals: product name and description. Supporting signals: product type, intended use, key active ingredient/material.
2. IGNORE BRAND COMPLETELY: Brand is metadata, not generic identity. Different brands of the same product type and active ingredient can represent the SAME generic catalog product.
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
        limit=5,
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

    # An exact normalized name is a safe, deterministic shortcut for the
    # unambiguous case. Ambiguous names still go to Qwen; no product aliases
    # or language-specific mappings are embedded here.
    incoming_tokens = set(extract_search_tokens(product_name))
    exact_name_candidates = [
        candidate for candidate in candidates
        if incoming_tokens and set(extract_search_tokens(candidate.name or "")) == incoming_tokens
    ]
    if exact_name_candidates:
        best = exact_name_candidates[0]
        return IdentityOutput(
            decision=IdentityDecision.MATCH,
            matched_catalog_id=best.id,
            confidence=0.99,
            reason="The normalized product name matches an existing catalog item.",
            identity_factors=["normalized_name_match"],
        )

    candidate_ids = {c.id for c in candidates}
    cand_dicts = [
        {
            "id": c.id,
            "name": c.name,
            "category": c.category or "",
            "product_details": c.product_details or "",
        }
        for c in candidates
    ]

    prompt = build_identity_prompt(
        product_name=product_name,
        product_details=product_details,
        category=category,
        candidates=cand_dicts,
    )

    llm_resp = generate_structured(
        prompt=prompt,
        model="qwen3:8b",
        timeout=30.0,
    )

    if not llm_resp.get("success") or not llm_resp.get("data"):
        err = llm_resp.get("error") or "LLM generation failed"
        logger.warning(
            f"[IDENTITY_LLM_DOWN_FALLBACK] LLM error for '{product_name}': {err}. "
            "Falling back safely to UNCERTAIN_MATCH."
        )
        # LLM down -> UNCERTAIN, pipeline does not fail
        return IdentityOutput(
            decision=IdentityDecision.UNCERTAIN_MATCH,
            matched_catalog_id=None,
            confidence=0.0,
            reason=f"LLM evaluation unavailable: {err}",
            identity_factors=["llm_error_fallback"],
        )

    data = llm_resp["data"]

    # ── Python Validation (Pydantic + candidate containment) ───────────────
    try:
        raw_decision = str(data.get("decision", "")).strip().upper()
        raw_decision = {"MATCH_EXISTING": "MATCH", "NEW_PRODUCT": "NOT_MATCH"}.get(raw_decision, raw_decision)
        if raw_decision not in [d.value for d in IdentityDecision]:
            raw_decision = IdentityDecision.UNCERTAIN_MATCH.value

        raw_id = data.get("matched_catalog_id")
        raw_conf = data.get("confidence", 0.0)

        # Validate numeric confidence in [0.0, 1.0]
        try:
            conf = float(raw_conf)
            conf = max(0.0, min(1.0, conf))
        except (ValueError, TypeError):
            conf = 0.5
            raw_decision = IdentityDecision.UNCERTAIN_MATCH.value

        validated_id = None
        if raw_decision == IdentityDecision.MATCH.value:
            if raw_id is not None:
                try:
                    int_id = int(raw_id)
                    if int_id in candidate_ids:
                        validated_id = int_id
                    else:
                        logger.warning(
                            f"[IDENTITY_VALIDATION_FAILED] LLM matched ID {int_id} "
                            f"not in candidate IDs {candidate_ids}. Forcing UNCERTAIN_MATCH."
                        )
                        raw_decision = IdentityDecision.UNCERTAIN_MATCH.value
                except (ValueError, TypeError):
                    raw_decision = IdentityDecision.UNCERTAIN_MATCH.value
            else:
                raw_decision = IdentityDecision.UNCERTAIN_MATCH.value

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

    except Exception as val_exc:
        logger.error(f"[IDENTITY_VALIDATION_ERROR] {val_exc}", exc_info=True)
        return IdentityOutput(
            decision=IdentityDecision.UNCERTAIN_MATCH,
            matched_catalog_id=None,
            confidence=0.0,
            reason=f"Validation error: {val_exc}",
            identity_factors=["validation_exception"],
        )


def determine_product_lifecycle(
    db: Session,
    product_name: str,
    category: str = "",
    brand: str = "",
    product_details: str = "",
) -> Dict[str, Any]:
    """
    Catalog lifecycle resolution:
    - MATCH_EXISTING -> reuse the row. Fresh -> reuse data. Stale -> same row, re-scrape.
      Never create a duplicate for staleness.
    - UNCERTAIN_MATCH -> never silently merge. Create new row marked uncertain / return for review.
    - NEW_PRODUCT -> create a ProductCatalog row.
    """
    identity = evaluate_product_identity(
        db=db,
        product_name=product_name,
        product_details=product_details,
        category=category,
        brand=brand,
    )

    if identity.decision == IdentityDecision.MATCH_EXISTING and identity.matched_catalog_id:
        catalog_product_id = identity.matched_catalog_id
        is_fresh = has_fresh_competitor_data(
            db=db,
            catalog_product_id=catalog_product_id,
            threshold_days=SCRAPE_FRESHNESS_THRESHOLD_DAYS,
        )
        if is_fresh:
            logger.info(
                f"[FRESH_DATA_REUSED] Competitor data fresh (id={catalog_product_id}). "
                "Reusing existing catalog product without scraping."
            )
            decision = "REUSE_EXISTING"
        else:
            logger.info(
                f"[DATA_EXPIRED_REFRESH_STARTED] Competitor data stale (id={catalog_product_id}). "
                "Reusing catalog row and initiating re-scrape."
            )
            decision = "REFRESH_EXISTING"

        return {
            "decision": decision,
            "catalog_product_id": catalog_product_id,
            "match_confidence": identity.confidence,
            "identity_decision": identity.decision.value,
            "reason": identity.reason,
            "uncertain": False,
        }

    elif identity.decision == IdentityDecision.UNCERTAIN_MATCH:
        logger.warning(
            f"[UNCERTAIN_MATCH_DETECTED] Possible match for '{product_name}' "
            f"(candidate={identity.matched_catalog_id}, conf={identity.confidence:.2f}). "
            "Never silently merging; creating isolated new catalog entry."
        )
        return {
            "decision": "CREATE_NEW",
            "catalog_product_id": None,
            "match_confidence": identity.confidence,
            "identity_decision": identity.decision.value,
            "reason": identity.reason,
            "uncertain": True,
            "suggested_match_id": identity.matched_catalog_id,
        }

    else:
        logger.info(f"[NEW_PRODUCT_CREATED] '{product_name}' resolved as NEW_PRODUCT.")
        return {
            "decision": "CREATE_NEW",
            "catalog_product_id": None,
            "match_confidence": identity.confidence,
            "identity_decision": identity.decision.value,
            "reason": identity.reason,
            "uncertain": False,
        }
