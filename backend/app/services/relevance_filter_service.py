"""
relevance_filter_service.py
===========================
Competitor relevance filter matching product TYPE and intended use.

Pipeline:
1. Cheap lexical checks:
   - Conflicting product form / type rejection (e.g. shampoo/soap/wash rejected for hair oil)
   - Obvious type matches accepted
2. ONE batched Qwen3 call for borderline survivors
   - Title hash caching to avoid duplicate evaluations
   - Fallback to lexical results if Qwen3 is unavailable
"""

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.llm_service import generate_structured
from app.config import OLLAMA_RELEVANCE_TIMEOUT

logger = logging.getLogger(__name__)

# In-memory cache: md5(target_type + "::" + title) -> bool
_RELEVANCE_CACHE: Dict[str, bool] = {}

# Product type keywords for fast lexical classification
PRODUCT_TYPES = [
    "oil", "shampoo", "soap", "cream", "serum", "gel", "face wash", "body wash",
    "wash", "lotion", "conditioner", "cleanser", "scrub", "mask", "cake", "tablet",
    "capsule", "syrup", "powder", "tea", "coffee", "perfume", "spray"
]
FORMULATION_TERMS = {
    "aloe", "aloe vera", "ashwagandha", "turmeric", "neem", "tea tree",
    "salicylic acid", "benzoyl peroxide", "niacinamide", "retinol",
    "hyaluronic acid", "vitamin c", "charcoal", "sandalwood", "rosehip",
    "hibiscus", "coconut", "goat milk", "vitamin e", "cucumber", "jojoba", "argan",
    "rosemary",
}
_MATCH_MARKER = "[[PRICEPULSE_MATCH:"


def mark_match_quality(details: str | None, quality: str | None) -> str | None:
    if quality not in {"exact", "alternative"}:
        return details
    raw = (details or "").split(_MATCH_MARKER, 1)[0].rstrip()
    return f"{raw}\n{_MATCH_MARKER}{quality}]]".strip()


def get_match_quality(details: str | None) -> str:
    if details and _MATCH_MARKER in details:
        value = details.split(_MATCH_MARKER, 1)[1].split("]]", 1)[0]
        return value if value in {"exact", "alternative"} else "uncertain"
    return "exact"  # Existing records predate explicit classification.


def strip_match_marker(details: str | None) -> str | None:
    if not details or _MATCH_MARKER not in details:
        return details
    prefix, rest = details.split(_MATCH_MARKER, 1)
    suffix = rest.split("]]", 1)[1] if "]]" in rest else ""
    return (prefix + suffix).strip() or None


def _detect_types(text: str) -> Set[str]:
    """Detect presence of known product types in text."""
    t_lower = text.lower()
    found = set()
    for pt in PRODUCT_TYPES:
        pattern = r"\b" + re.escape(pt) + r"\b"
        if re.search(pattern, t_lower):
            found.add(pt)
    return found


def _cache_key(target: str, candidate_title: str, target_details: str = "", candidate_details: str = "") -> str:
    combined = f"{target.strip().lower()}::{target_details.strip().lower()}::{candidate_title.strip().lower()}::{candidate_details.strip().lower()}"
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


def classify_candidate(target_name: str, candidate_title: str, target_details: str = "",
                       candidate_details: str = "") -> Tuple[str, str]:
    """Apply deterministic safety checks before a candidate can affect pricing."""
    target_text = f"{target_name} {target_details}".casefold().replace("aloevera", "aloe vera")
    candidate_text = f"{candidate_title} {candidate_details}".casefold().replace("aloevera", "aloe vera")
    # Explicitly exclude materially different product forms before shared
    # ingredient words (such as "rosemary") can make a listing look relevant.
    # Price is intentionally not considered by these checks.
    target_is_serum = bool(re.search(r"\bserum\b", target_name.casefold()))
    candidate_title_text = (candidate_title or "").casefold()
    if target_is_serum and re.search(r"\b(?:kit|set|bundle|combo)\b", candidate_title_text):
        return "rejected", "Listing is a kit, set, bundle, or combo rather than an individual serum."
    if target_is_serum and re.search(r"\bessential\s+oil\b", candidate_title_text):
        return "rejected", "Essential oil is a different product form from a hair serum."
    if target_is_serum and re.search(r"\bhair\s+oil\b", candidate_title_text) and not re.search(r"\bhair\s+serum\b", candidate_title_text):
        return "rejected", "Plain hair oil is a different product form from a hair serum."
    target_title_types = _detect_types(target_name)
    candidate_title_types = _detect_types(candidate_title or "")
    # Product descriptions often contain recommendations, reviews, and use
    # instructions that mention other forms (e.g. essential oil to use with a
    # serum). Prefer the explicit listing title for product-form classification.
    target_types = target_title_types or _detect_types(target_text)
    candidate_types = candidate_title_types or _detect_types(candidate_text)
    shared_types = target_types & candidate_types
    if target_types and candidate_types and (not shared_types or candidate_types - target_types):
        return "rejected", "Conflicting product form/type."
    if target_types and not candidate_types:
        return "uncertain", "Candidate product form is not established."
    target_formulas = {term for term in FORMULATION_TERMS if term in target_text}
    candidate_formulas = {term for term in FORMULATION_TERMS if term in candidate_text}
    if "aloe" in target_formulas:
        target_formulas.discard("aloe")
        target_formulas.add("aloe vera")
    if "aloe" in candidate_formulas:
        candidate_formulas.discard("aloe")
        candidate_formulas.add("aloe vera")
    if target_formulas and candidate_formulas and target_formulas != candidate_formulas:
        return "rejected", "Meaningful formulation or ingredient conflict."
    if target_formulas and not candidate_formulas:
        return "uncertain", "Candidate does not establish the target formulation."
    if not target_formulas and candidate_formulas:
        return "uncertain", "Candidate formulation is not established by the target evidence."
    target_tokens = set(re.findall(r"[a-zA-Z0-9]+", target_name.casefold()))
    candidate_tokens = set(re.findall(r"[a-zA-Z0-9]+", candidate_title.casefold()))
    target_tokens = {part for token in target_tokens for part in ({"aloe", "vera"} if token == "aloevera" else {token})}
    candidate_tokens = {part for token in candidate_tokens for part in ({"aloe", "vera"} if token == "aloevera" else {token})}
    generic = {"gel", "cream", "soap", "oil", "shampoo", "product", "natural", "organic", "pure"}
    target_tokens -= generic
    candidate_tokens -= generic
    common_tokens = target_tokens & candidate_tokens
    if not common_tokens or len(common_tokens) / max(1, len(target_tokens)) < 0.75:
        if not target_formulas or target_formulas != candidate_formulas:
            return "uncertain", "No meaningful identity token is shared."
        return "alternative", "Formulation and product type match; brand/title wording differs."
    return ("exact" if target_tokens == candidate_tokens else "alternative",
            "Compatible type and formulation; brand difference is allowed.")


def lexical_relevance_check(target_name: str, candidate_title: str, target_details: str = "",
                            candidate_details: str = "") -> Tuple[Optional[bool], str]:
    """
    Fast lexical evaluation.
    Returns:
        (True, reason) -> Definite accept
        (False, reason) -> Definite reject
        (None, reason) -> Borderline (needs LLM)
    """
    quality, reason = classify_candidate(target_name, candidate_title, target_details, candidate_details)
    if quality == "rejected":
        return False, reason
    if quality == "uncertain":
        return None, reason
    return True, reason


def filter_candidate_products(
    target_name: str,
    target_category: str = "",
    candidates: List[Dict[str, Any]] = None,
    target_details: str = "",
) -> List[Dict[str, Any]]:
    """
    Filter candidate competitor products to keep only relevant competitors.
    Uses cheap lexical checks first, then ONE batched Qwen3 call for borderline items.
    """
    if not candidates:
        return []

    accepted: List[Dict[str, Any]] = []
    borderline: List[Tuple[int, Dict[str, Any]]] = []

    for idx, cand in enumerate(candidates):
        title = cand.get("product_title") or cand.get("product_name") or ""
        details = cand.get("product_details") or ""
        if isinstance(details, (list, dict)):
            details = json.dumps(details, ensure_ascii=False)
        ckey = _cache_key(target_name, title, target_details, str(details))
        if ckey in _RELEVANCE_CACHE:
            is_rel = _RELEVANCE_CACHE[ckey]
            quality, _ = classify_candidate(target_name, title, target_details, str(details))
            if is_rel and quality in {"exact", "alternative"}:
                accepted.append({**cand, "match_quality": quality})
            continue
        quality, _ = classify_candidate(target_name, title, target_details, str(details))
        decision, reason = lexical_relevance_check(target_name, title, target_details, str(details))
        if decision is True:
            _RELEVANCE_CACHE[ckey] = True
            accepted.append({**cand, "match_quality": quality})
        elif decision is False:
            _RELEVANCE_CACHE[ckey] = False
            logger.debug(f"[RELEVANCE_REJECTED_LEXICAL] '{title}' - {reason}")
        else:
            borderline.append((idx, cand))

    # Batched Qwen3 call for borderline items
    if borderline:
        logger.info(
            f"[RELEVANCE_BATCH_LLM_STARTED] target='{target_name}' borderline_count={len(borderline)}"
        )
        batch_items = [
            {"index": i, "title": c.get("product_title") or c.get("product_name") or ""}
            for i, c in borderline
        ]

        prompt = f"""TARGET PRODUCT:
- Name: "{target_name}"
- Category: "{target_category}"

CANDIDATE COMPETITOR TITLES TO EVALUATE:
{json.dumps(batch_items, indent=2)}

TASK:
Determine if each candidate is a genuine competitor sharing the same product TYPE and INTENDED USE.
Reject candidates with different product forms (e.g. shampoo/soap/cream vs hair oil; tablet vs tea).

OUTPUT FORMAT (JSON object with 'results' array):
{{
  "results": [
    {{
      "index": <int matching candidate index>,
      "relevant": <true or false>,
      "reason": "<short explanation>"
    }}
  ]
}}"""

        llm_resp = generate_structured(
            prompt=prompt,
            timeout=OLLAMA_RELEVANCE_TIMEOUT,
            operation="scout_relevance",
        )

        llm_success = llm_resp.get("success") and llm_resp.get("data")
        results_map = {}
        if llm_success:
            raw_results = llm_resp["data"].get("results", [])
            for item in raw_results:
                if isinstance(item, dict) and "index" in item:
                    results_map[item["index"]] = bool(item.get("relevant", False))

        for idx, cand in borderline:
            title = cand.get("product_title") or cand.get("product_name") or ""
            details = cand.get("product_details") or ""
            if isinstance(details, (list, dict)):
                details = json.dumps(details, ensure_ascii=False)
            ckey = _cache_key(target_name, title, target_details, str(details))
            if idx in results_map:
                is_rel = results_map[idx]
                quality, _ = classify_candidate(target_name, title, target_details, str(details))
                # The model can reject a listing, but cannot establish identity
                # where deterministic evidence remains ambiguous.
                accepted_flag = is_rel and quality in {"exact", "alternative"}
                _RELEVANCE_CACHE[ckey] = accepted_flag
                if accepted_flag:
                    accepted.append({**cand, "match_quality": quality})
            else:
                # Failure cannot turn a borderline candidate into verified evidence.
                _RELEVANCE_CACHE[ckey] = False

    logger.info(
        f"[RELEVANCE_FILTER_COMPLETED] target='{target_name}' "
        f"input={len(candidates)} kept={len(accepted)}"
    )
    return accepted
