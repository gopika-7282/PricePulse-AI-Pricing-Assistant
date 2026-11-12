"""Conservative pack-quantity parsing and normalization helpers."""
import json
import re

_FACTORS = {"mg": ("mass", 0.001), "g": ("mass", 1.0), "kg": ("mass", 1000.0),
            "ml": ("volume", 1.0), "l": ("volume", 1000.0)}
_UNIT = r"mg|kg|g|ml|l"
_SINGLE = re.compile(rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT})\b", re.I)
_MULTIPACK = re.compile(
    rf"(?:(?P<prefix>\d+)\s*[x×]\s*(?P<pvalue>\d+(?:\.\d+)?)\s*(?P<punit>{_UNIT})\b)"
    rf"|(?:(?P<tvalue>\d+(?:\.\d+)?)\s*(?P<tunit>{_UNIT})\s*(?:[x×]\s*(?P<tcount>\d+)|pack\s+of\s+(?P<ofcount>\d+))\b)", re.I)
_UNCERTAINTY_MARKER = "[[PRICEPULSE_QUANTITY_UNCERTAIN:"
_EVIDENCE_MARKER = "[[PRICEPULSE_QUANTITY_EVIDENCE:"


def normalize_quantity(value: float, unit: str) -> tuple[float, str] | None:
    canonical = (unit or "").strip().casefold()
    if canonical not in _FACTORS or value is None or value <= 0:
        return None
    dimension, factor = _FACTORS[canonical]
    return value * factor, dimension


def _one_quantity(text: str, source: str) -> dict | None:
    """Parse a single, explicit pack quantity from one evidence source."""
    text = text or ""
    matches = list(_SINGLE.finditer(text))
    if not matches:
        return None

    gross_matches = []
    # Explicit gross/shipping/package weights are evidence, not sale quantity.
    net_matches = []
    for match in matches:
        context = text[max(0, match.start() - 32):match.start()].casefold()
        if re.search(r"(?:gross|shipping|package|packaging)\s*(?:weight)?\s*[:=-]?\s*$", context):
            gross_matches.append(match)
            continue
        if re.search(r"net\s*(?:quantity|weight|content)?\s*[:=-]?\s*$", context):
            net_matches.append(match)
    if net_matches:
        matches = net_matches
    elif gross_matches and len(gross_matches) == len(matches):
        m = gross_matches[0]
        return {"ambiguous": True, "source": source,
                "gross_weight": {"value": float(m.group("value")), "unit": m.group("unit").casefold()},
                "candidates": [m.group(0)], "reason": "Only gross/package weight was identified."}
    gross_weight = ({"value": float(gross_matches[0].group("value")),
                     "unit": gross_matches[0].group("unit").casefold()}
                    if gross_matches else None)

    # Pair syntax is explicit in either ordering: 2 x 100 g, 100 g x 2,
    # and 100 g pack of 3. It wins over the individual quantity match.
    pack = _MULTIPACK.search(text)
    if pack:
        unmatched = [m for m in matches if not (pack.start() <= m.start() and m.end() <= pack.end())
                     and m not in gross_matches]
        if unmatched:
            return {"ambiguous": True, "source": source,
                    "gross_weight": gross_weight,
                    "candidates": [m.group(0) for m in matches],
                    "reason": "Additional quantity values are not explained by the pack expression."}
        if pack.group("prefix"):
            count, value, unit = int(pack.group("prefix")), float(pack.group("pvalue")), pack.group("punit")
        else:
            count = int(pack.group("tcount") or pack.group("ofcount"))
            value, unit = float(pack.group("tvalue")), pack.group("tunit")
        return {"quantity_value": value, "quantity_unit": unit.casefold(), "pack_count": count,
                "total_quantity": value * count, "total_quantity_unit": unit.casefold(),
                "source": source, "gross_weight": gross_weight}

    # Multiple unrelated quantities in one source are not safe to resolve
    # (for example, active strength plus net contents).
    if len(matches) != 1:
        return {"ambiguous": True, "source": source, "candidates": [m.group(0) for m in matches]}
    match = matches[0]
    value, unit = float(match.group("value")), match.group("unit").casefold()
    return {"quantity_value": value, "quantity_unit": unit, "pack_count": 1,
            "total_quantity": value, "total_quantity_unit": unit,
            "source": source, "gross_weight": gross_weight}


def reconcile_quantity(title: str | None = None, details: str | None = None,
                       scraper_metadata: dict | None = None) -> dict:
    """Return quantity fields only when independent evidence agrees.

    Source values are retained in ``evidence`` for auditability. Conflicting or
    internally ambiguous evidence returns ``status=ambiguous`` and no usable
    quantity fields.
    """
    candidates = []
    for source, text in (("title", title), ("product_details", details)):
        parsed = _one_quantity(str(text or ""), source)
        if parsed:
            candidates.append(parsed)
    if scraper_metadata:
        qv, qu = scraper_metadata.get("quantity_value"), scraper_metadata.get("quantity_unit")
        count = scraper_metadata.get("pack_count") or 1
        total, total_unit = scraper_metadata.get("total_quantity"), scraper_metadata.get("total_quantity_unit") or qu
        if qv is not None or total is not None:
            if qv is not None and total is not None:
                expected = normalize_quantity(float(qv) * int(count), qu or total_unit)
                actual = normalize_quantity(float(total), total_unit or qu)
                if not expected or not actual or expected[1] != actual[1] or not __import__("math").isclose(expected[0], actual[0], rel_tol=0.001):
                    candidates.append({"ambiguous": True, "source": "scraper_metadata",
                                       "reason": "Scraper per-item, pack-count, and total quantities conflict."})
            if total is None and qv is not None:
                total = float(qv) * int(count)
            candidates.append({"quantity_value": float(qv if qv is not None else total),
                "quantity_unit": (qu or total_unit or "").casefold(), "pack_count": int(count),
                "total_quantity": float(total), "total_quantity_unit": (total_unit or qu or "").casefold(),
                "source": "scraper_metadata", "gross_weight": None})
    evidence = [{k: v for k, v in c.items() if k != "ambiguous"} for c in candidates]
    if any(c.get("ambiguous") for c in candidates):
        return {"status": "ambiguous", "evidence": evidence,
                "reason": "Multiple quantity values in one source could not be distinguished."}
    if not candidates:
        return {"status": "unknown", "evidence": []}
    normalized = [normalize_quantity(c["total_quantity"], c["total_quantity_unit"]) for c in candidates]
    if any(n is None for n in normalized) or any(n[1] != normalized[0][1] or not __import__("math").isclose(n[0], normalized[0][0], rel_tol=0.001) for n in normalized[1:]):
        return {"status": "ambiguous", "evidence": evidence,
                "reason": "Title, details, or scraper quantity evidence conflicts."}
    # Prefer explicit title/detail per-item counts over scraper metadata where
    # both agree on total. Preserve each source in the provenance array.
    chosen = next((c for c in candidates if c["source"] in {"title", "product_details"}), candidates[0])
    return {"status": "resolved", "evidence": evidence,
            **{k: chosen[k] for k in ("quantity_value", "quantity_unit", "pack_count", "total_quantity", "total_quantity_unit")}}


def extract_quantity(text: str | None) -> dict | None:
    """Backward-compatible single-source extraction with explicit ambiguity."""
    parsed = reconcile_quantity(title=text)
    if parsed["status"] == "ambiguous":
        return parsed
    if parsed["status"] != "resolved":
        return None
    return {key: parsed[key] for key in
            ("quantity_value", "quantity_unit", "pack_count", "total_quantity", "total_quantity_unit")}


def mark_quantity_uncertain(details: str | None, evidence: list, reason: str | None = None) -> str:
    """Store a compact provenance marker in the existing details field."""
    raw = (details or "").split(_UNCERTAINTY_MARKER, 1)[0].rstrip()
    raw = raw.split(_EVIDENCE_MARKER, 1)[0].rstrip()
    payload = json.dumps({"evidence": evidence, "reason": reason}, ensure_ascii=False, separators=(",", ":"))
    return f"{raw}\n{_UNCERTAINTY_MARKER}{payload}]]".strip()


def quantity_is_uncertain(details: str | None) -> bool:
    return bool(details and _UNCERTAINTY_MARKER in details)


def listing_quantity_is_uncertain(listing) -> bool:
    """Reconcile existing persisted rows too, including pre-marker records."""
    details = getattr(listing, "product_details", None)
    if quantity_is_uncertain(details):
        return True
    metadata = {key: getattr(listing, key, None) for key in
                ("quantity_value", "quantity_unit", "pack_count", "total_quantity", "total_quantity_unit")}
    reconciled = reconcile_quantity(
        title=getattr(listing, "product_name", None),
        details=strip_quantity_marker(details),
        scraper_metadata=metadata,
    )
    return reconciled["status"] == "ambiguous"


def listing_quantity_reason(listing) -> str | None:
    details = getattr(listing, "product_details", None)
    if quantity_is_uncertain(details):
        return quantity_uncertainty_reason(details)
    metadata = {key: getattr(listing, key, None) for key in
                ("quantity_value", "quantity_unit", "pack_count", "total_quantity", "total_quantity_unit")}
    reconciled = reconcile_quantity(
        title=getattr(listing, "product_name", None),
        details=strip_quantity_marker(details),
        scraper_metadata=metadata,
    )
    return reconciled.get("reason") if reconciled["status"] == "ambiguous" else None


def mark_quantity_provenance(details: str | None, evidence: list) -> str:
    raw = (details or "").split(_EVIDENCE_MARKER, 1)[0].rstrip()
    payload = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return f"{raw}\n{_EVIDENCE_MARKER}{payload}]]".strip()


def clear_quantity_uncertainty(details: str | None) -> str | None:
    if not quantity_is_uncertain(details):
        return details
    return (details or "").split(_UNCERTAINTY_MARKER, 1)[0].rstrip() or None


def quantity_uncertainty_reason(details: str | None) -> str | None:
    if not details or _UNCERTAINTY_MARKER not in details:
        return None
    raw = details.split(_UNCERTAINTY_MARKER, 1)[1].split("]]", 1)[0]
    try:
        return json.loads(raw).get("reason") or "Listing quantity is uncertain."
    except (ValueError, TypeError):
        return "Listing quantity is uncertain."


def strip_quantity_marker(details: str | None) -> str | None:
    if not details:
        return details
    for marker in (_UNCERTAINTY_MARKER, _EVIDENCE_MARKER):
        if marker in details:
            prefix, rest = details.split(marker, 1)
            # Evidence payloads may themselves end in `]`; split at the final
            # two brackets so the payload's closing bracket is not left behind.
            suffix = rest.rsplit("]]", 1)[1] if "]]" in rest else ""
            details = (prefix + suffix).strip()
    return details or None


def price_at_target_pack(price: float, target_value: float, target_unit: str,
                         quantity_value: float | None, quantity_unit: str | None,
                         pack_count: int | None = None,
                         total_quantity: float | None = None,
                         total_quantity_unit: str | None = None,
                         quantity_uncertain: bool = False) -> float | None:
    """Return target-pack equivalent only for known, unambiguous dimensions."""
    if quantity_uncertain:
        return None
    target = normalize_quantity(target_value, target_unit)
    observed_value = total_quantity if total_quantity is not None else quantity_value
    observed_unit = total_quantity_unit or quantity_unit
    if total_quantity is None and observed_value is not None:
        observed_value *= pack_count or 1
    observed = normalize_quantity(observed_value, observed_unit) if observed_value and observed_unit else None
    if not target or not observed or target[1] != observed[1]:
        return None
    return float(price) / observed[0] * target[0]
