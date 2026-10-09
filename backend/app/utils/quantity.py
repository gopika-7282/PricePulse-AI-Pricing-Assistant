"""Pack quantity parsing and dimension-safe normalization helpers."""
import re

_FACTORS = {"mg": ("mass", 0.001), "g": ("mass", 1.0), "kg": ("mass", 1000.0),
            "ml": ("volume", 1.0), "l": ("volume", 1000.0)}
_QUANTITY = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(mg|kg|g|ml|l)\b", re.I)


def normalize_quantity(value: float, unit: str) -> tuple[float, str] | None:
    canonical = (unit or "").strip().casefold()
    if canonical not in _FACTORS or value <= 0:
        return None
    dimension, factor = _FACTORS[canonical]
    return value * factor, dimension


def extract_quantity(text: str | None) -> dict | None:
    """Extract an explicit pack size; never infer a missing unit or quantity."""
    match = _QUANTITY.search(text or "")
    if not match:
        return None
    count = int(match.group(1) or 1)
    value = float(match.group(2))
    unit = match.group(3).casefold()
    if value <= 0 or count <= 0:
        return None
    return {"quantity_value": value, "quantity_unit": unit, "pack_count": count,
            "total_quantity": value * count, "total_quantity_unit": unit}


def price_at_target_pack(price: float, target_value: float, target_unit: str,
                         quantity_value: float | None, quantity_unit: str | None,
                         pack_count: int | None = None,
                         total_quantity: float | None = None,
                         total_quantity_unit: str | None = None) -> float | None:
    """Return target-pack equivalent price only for compatible known dimensions."""
    target = normalize_quantity(target_value, target_unit)
    observed_value = total_quantity if total_quantity is not None else quantity_value
    observed_unit = total_quantity_unit or quantity_unit
    if total_quantity is None and observed_value is not None:
        observed_value *= pack_count or 1
    observed = normalize_quantity(observed_value, observed_unit) if observed_value and observed_unit else None
    if not target or not observed or target[1] != observed[1]:
        return None
    return float(price) / observed[0] * target[0]
