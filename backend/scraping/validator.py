"""
validator.py
============
Pydantic-based validation layer for scraped competitor product data.

Requirements & Standardized Format:
- platform: non-empty string (e.g., "Flipkart")
- product_title: non-empty string
- product_url: non-empty valid HTTP/HTTPS URL string
- price: float > 0.0
- rating: float in range [0.0, 5.0], or None
- availability: boolean when observed, otherwise None
- scraped_at: ISO-8601 string timestamp

If validation fails for any mandatory field, the record is rejected and VALIDATION_FAILED log is emitted.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, ValidationError

logger = logging.getLogger(__name__)


class ScrapedProductSchema(BaseModel):
    """
    Pydantic validation schema for scraped competitor product objects.
    Enforces required field types, positive prices, valid URLs, and rating bounds.
    """
    platform: str = Field(..., min_length=1, description="Retail platform name")
    product_title: str = Field(..., min_length=1, description="Scraped product name/title")
    product_url: str = Field(..., min_length=1, description="Cleaned product page URL")
    price: float = Field(..., gt=0, description="Product price in INR (must be > 0)")
    rating: Optional[float] = Field(default=None, description="Product rating [0.0 - 5.0]")
    availability: Optional[bool] = Field(default=None, description="Stock availability when explicitly reported")
    scraped_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO-8601 UTC timestamp of scraping execution"
    )
    product_details: Optional[Any] = Field(
        default_factory=list,
        description="Scraped product details or specifications"
    )
    quantity_value: Optional[float] = Field(default=None, gt=0)
    quantity_unit: Optional[str] = None
    pack_count: Optional[int] = Field(default=None, gt=0)
    total_quantity: Optional[float] = Field(default=None, gt=0)
    total_quantity_unit: Optional[str] = None

    @field_validator("platform", "product_title", "product_url", mode="before")
    @classmethod
    def strip_strings(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("product_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"Invalid URL protocol: '{v[:30]}'")
        return v

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        try:
            r = float(v)
            if 0.0 <= r <= 5.0:
                return round(r, 2)
            else:
                logger.warning(f"[VALIDATION] Rating {r} out of bounds [0.0, 5.0]. Normalizing to None.")
                return None
        except (ValueError, TypeError):
            return None


def validate_scraped_product(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Validate a raw scraped dictionary against ScrapedProductSchema.
    Normalizes key aliases (e.g. platform_name -> platform, product_name -> product_title).
    Returns standardized product dictionary if valid, or None if validation fails.
    """
    if not isinstance(data, dict):
        logger.warning("[VALIDATION_FAILED] Input payload is not a dictionary.")
        return None

    # Map internal/legacy keys to standardized schema field names
    raw_payload = {
        "platform": data.get("platform") or data.get("platform_name") or "Flipkart",
        "product_title": data.get("product_title") or data.get("product_name"),
        "product_url": data.get("product_url"),
        "price": data.get("price"),
        "rating": data.get("rating"),
        "availability": data.get("availability"),
        "scraped_at": data.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        "product_details": data.get("product_details", []),
        "quantity_value": data.get("quantity_value"), "quantity_unit": data.get("quantity_unit"),
        "pack_count": data.get("pack_count"), "total_quantity": data.get("total_quantity"),
        "total_quantity_unit": data.get("total_quantity_unit"),
    }

    try:
        model = ScrapedProductSchema(**raw_payload)
        res = model.model_dump()
        res["price"] = round(res["price"], 2)
        return res
    except ValidationError as ve:
        title_snippet = str(raw_payload.get("product_title") or raw_payload.get("product_url") or "Unknown")[:40]
        logger.warning(f"[VALIDATION_FAILED] Rejected '{title_snippet}': {ve.errors()}")
        return None
    except Exception as e:
        logger.warning(f"[VALIDATION_FAILED] Unexpected validation error: {e}")
        return None


# Backward compatibility alias
validate_product = validate_scraped_product


def validate_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Validate a list of raw scraped product dictionaries.
    Filters out invalid entries and returns standardized dicts.
    """
    validated_list = []
    for item in products:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "blocked":
            continue
        valid_dict = validate_scraped_product(item)
        if valid_dict:
            validated_list.append(valid_dict)

    logger.info(f"[VALIDATION] Validation complete: {len(validated_list)}/{len(products)} products passed.")
    return validated_list
