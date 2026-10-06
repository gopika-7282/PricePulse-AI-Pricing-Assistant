"""
scout.py
========
Multi-platform scraping orchestrator for PricePulse.
Active platforms: Flipkart and Amazon only (sequential).

Pipeline:
1. Focused queries (2-3 generic tokens from product name/type)
2. Extract candidates from search & detail pages
3. Dedupe by normalized URL and title
4. Relevance filter (lexical + batched Qwen3 for borderline)
5. Rank (order preserved)
6. Validate (Pydantic schema)
7. Top N (ceiling MAX_PRODUCTS = 10, never pad)
8. Stage-count logs: scraped -> deduped -> relevant -> final
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from scraping.flipkart_scraper import FlipkartScraper
from scraping.amazon_scraper import AmazonScraper
from scraping.validator import validate_products
from app.services.competitor_service import normalize_competitor_url
from app.services.relevance_filter_service import filter_candidate_products

logger = logging.getLogger(__name__)

HARD_MAX_PRODUCTS = 10


def process_platform_pipeline(
    platform_name: str,
    target_name: str,
    target_category: Optional[str],
    raw_products: List[Dict[str, Any]],
    max_products: int = 10,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Run the extraction post-processing pipeline:
    extract -> dedupe -> relevance filter -> rank -> validate -> top N
    Emits structured stage-count logs.
    """
    scraped_count = len(raw_products)

    # 1. Dedupe by normalized URL or title
    seen_keys = set()
    deduped: List[Dict[str, Any]] = []
    for p in raw_products:
        if not isinstance(p, dict):
            continue
        url = normalize_competitor_url(p.get("product_url"))
        title = (p.get("product_title") or p.get("product_name") or "").strip().lower()
        key = url or title
        if key and key not in seen_keys:
            seen_keys.add(key)
            deduped.append(p)
    deduped_count = len(deduped)

    # 2. Relevance filter
    relevant = filter_candidate_products(
        target_name=target_name,
        target_category=target_category or "",
        candidates=deduped,
    )
    relevant_count = len(relevant)

    # 3. Rank & ceiling cap (never pad)
    ceiling = min(max_products, HARD_MAX_PRODUCTS)
    ranked = relevant[:ceiling]

    # 4. Validate
    validated = validate_products(ranked)
    final_count = len(validated)

    logger.info(
        f"[{platform_name.upper()}_STAGE_COUNTS] scraped={scraped_count} "
        f"deduped={deduped_count} relevant={relevant_count} final={final_count}"
    )

    stage_counts = {
        "scraped": scraped_count,
        "deduped": deduped_count,
        "relevant": relevant_count,
        "final": final_count,
    }
    return validated, stage_counts


class ScoutScraper:
    """
    Multi-platform scraping orchestrator for Flipkart and Amazon.
    Sequential execution (not concurrent).
    """

    def __init__(self, headless: bool = True, max_retries: int = 2):
        self.headless = headless
        self.max_retries = max_retries
        self._flipkart = FlipkartScraper(headless=headless, max_retries=max_retries)
        self._amazon = AmazonScraper(headless=headless, max_retries=max_retries)

    async def _run_flipkart(
        self,
        product_name: str,
        category: Optional[str],
        product_details: Optional[str],
        max_products: int,
    ) -> Dict[str, Any]:
        """Execute Flipkart scrape with error isolation and pipeline filtering."""
        try:
            logger.info(f"[SCOUT] Running Flipkart scraper for: '{product_name}'")
            res = await self._flipkart.scrape_flipkart_with_retry(
                product_name=product_name,
                category=category,
                product_details=product_details,
                max_products=min(max_products, HARD_MAX_PRODUCTS),
            )
            if not isinstance(res, dict):
                res = {"platform": "Flipkart", "status": "SUCCESS" if res else "EMPTY", "products": res or []}

            status = res.get("status", "EMPTY")
            if status in ("BLOCKED", "TIMEOUT", "PARSE_ERROR"):
                return {
                    "platform": "Flipkart",
                    "status": status,
                    "products": [],
                    "error": res.get("error", f"Flipkart returned {status}"),
                }

            raw_prods = res.get("products", [])
            valid_prods, _ = process_platform_pipeline(
                platform_name="Flipkart",
                target_name=product_name,
                target_category=category,
                raw_products=raw_prods,
                max_products=max_products,
            )

            final_status = "SUCCESS" if valid_prods else "EMPTY"
            return {
                "platform": "Flipkart",
                "status": final_status,
                "products": valid_prods,
                "error": None if valid_prods else "No relevant products found",
            }
        except Exception as e:
            logger.error(f"[SCOUT] Flipkart runner error: {e}", exc_info=True)
            return {"platform": "Flipkart", "status": "PARSE_ERROR", "products": [], "error": str(e)}

    async def _run_amazon(
        self,
        product_name: str,
        category: Optional[str],
        product_details: Optional[str],
        max_products: int,
    ) -> Dict[str, Any]:
        """Execute Amazon scrape with error isolation and pipeline filtering."""
        try:
            logger.info(f"[SCOUT] Running Amazon scraper for: '{product_name}'")
            res = await self._amazon.scrape_amazon_with_retry(
                product_name=product_name,
                category=category,
                product_details=product_details,
                max_products=min(max_products, HARD_MAX_PRODUCTS),
            )
            if not isinstance(res, dict):
                res = {"platform": "Amazon", "status": "SUCCESS" if res else "EMPTY", "products": res or []}

            status = res.get("status", "EMPTY")
            if status in ("BLOCKED", "TIMEOUT", "PARSE_ERROR"):
                return {
                    "platform": "Amazon",
                    "status": status,
                    "products": [],
                    "error": res.get("error", f"Amazon returned {status}"),
                }

            raw_prods = res.get("products", [])
            valid_prods, _ = process_platform_pipeline(
                platform_name="Amazon",
                target_name=product_name,
                target_category=category,
                raw_products=raw_prods,
                max_products=max_products,
            )

            final_status = "SUCCESS" if valid_prods else "EMPTY"
            return {
                "platform": "Amazon",
                "status": final_status,
                "products": valid_prods,
                "error": None if valid_prods else "No relevant products found",
            }
        except Exception as e:
            logger.error(f"[SCOUT] Amazon runner error: {e}", exc_info=True)
            return {"platform": "Amazon", "status": "PARSE_ERROR", "products": [], "error": str(e)}

    async def scrape_sequential(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Sequential execution: Flipkart -> Amazon. Not concurrent.
        Returns per-platform results: { "Flipkart": {...}, "Amazon": {...} }
        """
        limit = min(max_products or HARD_MAX_PRODUCTS, HARD_MAX_PRODUCTS)

        # 1. Flipkart
        flipkart_res = await self._run_flipkart(product_name, category, product_details, limit)

        # 2. Amazon (sequential, isolated from Flipkart failure)
        amazon_res = await self._run_amazon(product_name, category, product_details, limit)

        return {
            "Flipkart": flipkart_res,
            "Amazon": amazon_res,
        }

    async def get_competitor_data(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
        platforms: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Backward-compatible entry point returning all products + block sentinels.
        """
        limit = min(max_products or HARD_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
        seq_res = await self.scrape_sequential(product_name, category, product_details, limit)

        out_products: List[Dict[str, Any]] = []
        for plat_name, res in seq_res.items():
            if res.get("status") == "BLOCKED":
                out_products.append({
                    "status": "blocked",
                    "platform": plat_name,
                    "error": res.get("error", "Blocked"),
                })
            else:
                out_products.extend(res.get("products", []))

        return out_products

    async def get_platform_statuses(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
        platforms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Extended entry point returning per-platform status and products."""
        limit = min(max_products or HARD_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
        seq_res = await self.scrape_sequential(product_name, category, product_details, limit)

        all_products = []
        platform_statuses = {}
        blocked = []
        failed = []

        for plat_name, res in seq_res.items():
            status = res.get("status", "EMPTY")
            platform_statuses[plat_name] = status
            if status == "BLOCKED":
                blocked.append(plat_name)
            elif status in ("PARSE_ERROR", "TIMEOUT"):
                failed.append(plat_name)
            elif status == "SUCCESS":
                all_products.extend(res.get("products", []))

        return {
            "products": all_products,
            "platform_statuses": platform_statuses,
            "total_products": len(all_products),
            "blocked_platforms": blocked,
            "failed_platforms": failed,
            "sequential_results": seq_res,
        }
