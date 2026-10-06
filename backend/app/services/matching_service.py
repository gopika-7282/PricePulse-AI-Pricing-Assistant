"""
matching_service.py
===================
Product Catalog Matching Service.

Routes matching calls to the new product identity engine
(app.services.product_identity_service) backed by PostgreSQL candidates and Qwen3.
"""

import logging
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session

from app.models.product_catalog import ProductCatalog
from app.services.product_identity_service import (
    evaluate_product_identity,
    IdentityDecision,
)

logger = logging.getLogger(__name__)


def find_catalog_match(
    db: Session,
    name: str,
    category: str = "",
    brand: str = "",
    details: str = "",
    threshold: Optional[float] = None,
    as_dict: bool = False,
) -> Any:
    """
    Find matching ProductCatalog row using deterministic Postgres candidates + Qwen3.

    Returns:
        ProductCatalog row if MATCH_EXISTING, else None (or dict if as_dict=True).
    """
    logger.info(
        f"[MATCH_REQUEST] name='{name}' brand='{brand}' category='{category}'"
    )

    identity = evaluate_product_identity(
        db=db,
        product_name=name,
        product_details=details,
        category=category,
        brand=brand,
    )

    if as_dict:
        return {
            "decision": identity.decision.value,
            "product_catalog_id": identity.matched_catalog_id,
            "confidence_score": identity.confidence,
            "reason": identity.reason,
            "identity_factors": identity.identity_factors,
        }

    if identity.decision == IdentityDecision.MATCH_EXISTING and identity.matched_catalog_id:
        catalog_obj = db.query(ProductCatalog).filter(ProductCatalog.id == identity.matched_catalog_id).first()
        return catalog_obj

    return None


def find_existing_product(
    db: Session,
    name: str,
    category: str = "",
    brand: str = "",
    product_details: str = "",
) -> Optional[ProductCatalog]:
    """Find existing catalog product row, returning ProductCatalog or None."""
    return find_catalog_match(
        db=db,
        name=name,
        category=category,
        brand=brand,
        details=product_details,
        as_dict=False,
    )
