"""
catalog_lifecycle_service.py
============================
Product Catalog Lifecycle Decision Layer.

Delegates lifecycle resolution to product_identity_service.
"""

import logging
from typing import Any, Dict
from sqlalchemy.orm import Session

from app.services.product_identity_service import (
    determine_product_lifecycle as identity_determine_lifecycle,
)

logger = logging.getLogger(__name__)


def determine_product_lifecycle(
    db: Session,
    product_name: str,
    category: str = "",
    brand: str = "",
    product_details: str = "",
) -> Dict[str, Any]:
    """
    Decide lifecycle action for incoming product request:
    - REUSE_EXISTING (fresh competitor data exists)
    - REFRESH_EXISTING (stale competitor data, needs re-scrape)
    - CREATE_NEW (new product or uncertain match)
    """
    return identity_determine_lifecycle(
        db=db,
        product_name=product_name,
        category=category,
        brand=brand,
        product_details=product_details,
    )
