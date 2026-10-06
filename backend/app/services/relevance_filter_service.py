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

logger = logging.getLogger(__name__)

# In-memory cache: md5(target_type + "::" + title) -> bool
_RELEVANCE_CACHE: Dict[str, bool] = {}

# Product type keywords for fast lexical classification
PRODUCT_TYPES = [
    "oil", "shampoo", "soap", "cream", "serum", "gel", "face wash", "body wash",
    "wash", "lotion", "conditioner", "cleanser", "scrub", "mask", "cake", "tablet",
    "capsule", "syrup", "powder", "tea", "coffee", "perfume", "spray"
]


def _detect_types(text: str) -> Set[str]:
    """Detect presence of known product types in text."""
    t_lower = text.lower()
    found = set()
    for pt in PRODUCT_TYPES:
        pattern = r"\b" + re.escape(pt) + r"\b"
        if re.search(pattern, t_lower):
            found.add(pt)
    return found


def _cache_key(target: str, candidate_title: str) -> str:
    combined = f"{target.strip().lower()}::{candidate_title.strip().lower()}"
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


def lexical_relevance_check(target_name: str, candidate_title: str) -> Tuple[Optional[bool], str]:
    """
    Fast lexical evaluation.
    Returns:
        (True, reason) -> Definite accept
        (False, reason) -> Definite reject
        (None, reason) -> Borderline (needs LLM)
    """
    target_lower = target_name.lower()
    cand_lower = candidate_title.lower()

    target_types = _detect_types(target_lower)
    cand_types = _detect_types(cand_lower)

    # If target has a specific product type, check for conflicts
    if target_types:
        # Check conflicting types
        # e.g. target is oil, cand is shampoo/soap/wash
        conflicts = cand_types - target_types
        # If cand has a conflicting type and does NOT share the target type
        if conflicts and not (cand_types & target_types):
            return False, f"Conflicting product type: candidate has {conflicts}, target is {target_types}"

        # If both share the primary type (e.g. both are oil or both are soap)
        if cand_types & target_types:
            # Check key ingredient words from target
            words = [w for w in re.findall(r"[a-zA-Z0-9]+", target_lower) if len(w) >= 4]
            matching_words = [w for w in words if w in cand_lower]
            if len(matching_words) >= 1:
                return True, f"Lexical match on type {cand_types & target_types} and words {matching_words}"

    return None, "Borderline product type or ambiguous wording"


def filter_candidate_products(
    target_name: str,
    target_category: str = "",
    candidates: List[Dict[str, Any]] = None,
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
        ckey = _cache_key(target_name, title)

        if ckey in _RELEVANCE_CACHE:
            is_rel = _RELEVANCE_CACHE[ckey]
            if is_rel:
                accepted.append(cand)
            continue

        decision, reason = lexical_relevance_check(target_name, title)
        if decision is True:
            _RELEVANCE_CACHE[ckey] = True
            accepted.append(cand)
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
            model="qwen3:8b",
            timeout=25.0,
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
            ckey = _cache_key(target_name, title)
            if idx in results_map:
                is_rel = results_map[idx]
                _RELEVANCE_CACHE[ckey] = is_rel
                if is_rel:
                    accepted.append(cand)
            else:
                # LLM down or missing from batch -> fallback to lexical permissiveness:
                # check if at least 1 keyword matches and no obvious conflict
                words = [w for w in re.findall(r"[a-zA-Z0-9]+", target_name.lower()) if len(w) >= 4]
                has_word = any(w in title.lower() for w in words)
                _RELEVANCE_CACHE[ckey] = has_word
                if has_word:
                    accepted.append(cand)

    logger.info(
        f"[RELEVANCE_FILTER_COMPLETED] target='{target_name}' "
        f"input={len(candidates)} kept={len(accepted)}"
    )
    return accepted
