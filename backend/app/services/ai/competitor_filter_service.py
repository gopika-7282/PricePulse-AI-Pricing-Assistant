"""
competitor_filter_service.py
============================
Connects Competitor Relevance Filtering with the Pricing Engine.

Filters raw CompetitorProduct database models using product type / intended use
relevance filtering (lexical checks + batched Qwen3).
"""

import logging
from typing import List
from sqlalchemy.orm import Session

from app.models.product_catalog import ProductCatalog
from app.models.competitor_product import CompetitorProduct
from app.services.relevance_filter_service import filter_candidate_products

logger = logging.getLogger(__name__)


def filter_relevant_competitors(
    db: Session,
    catalog_product_id: int,
    competitors: List[CompetitorProduct],
) -> List[CompetitorProduct]:
    """
    Filters a list of CompetitorProduct database models to include only those
    matching product type and intended use.
    """
    if not competitors:
        return []

    catalog_product = db.query(ProductCatalog).filter(ProductCatalog.id == catalog_product_id).first()
    if not catalog_product:
        logger.error(f"[COMPETITOR_FILTER_FAILED] Catalog product {catalog_product_id} not found.")
        return competitors

    logger.info(
        f"[COMPETITOR_FILTER_STARTED] catalog_product_id={catalog_product_id} "
        f"input_competitors={len(competitors)}"
    )

    comp_dicts = []
    for c in competitors:
        comp_dicts.append({
            "competitor_product_id": c.id,
            "product_title": c.product_name,
            "platform": c.platform_name,
            "price": c.price,
            "rating": c.rating,
            "availability": c.availability,
        })

    relevant_results = filter_candidate_products(
        target_name=catalog_product.name,
        target_category=catalog_product.category or "",
        candidates=comp_dicts,
    )

    relevant_ids = {r["competitor_product_id"] for r in relevant_results}
    filtered = [c for c in competitors if c.id in relevant_ids]

    logger.info(
        f"[RELEVANT_COMPETITORS_SELECTED] catalog_product_id={catalog_product_id} "
        f"selected={len(filtered)}/{len(competitors)}"
    )
    return filtered
