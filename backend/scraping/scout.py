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
import os
from typing import Any, Dict, List, Optional, Tuple

from scraping.flipkart_scraper import FlipkartScraper
from scraping.amazon_scraper import AmazonScraper
from scraping.myntra_scraper import MyntraScraper
from scraping.meesho_scraper import MeeshoScraper
from scraping.validator import validate_products
from app.services.competitor_service import normalize_competitor_url
from app.services.relevance_filter_service import filter_candidate_products

logger = logging.getLogger(__name__)

HARD_MAX_PRODUCTS = 10
PLATFORM_TIMEOUT_SECONDS = max(5.0, float(os.getenv("MARKETPLACE_TIMEOUT_SECONDS", "25")))
AMAZON_PLATFORM_TIMEOUT_SECONDS = max(
    PLATFORM_TIMEOUT_SECONDS,
    float(os.getenv("AMAZON_MARKETPLACE_TIMEOUT_SECONDS", "180")),
)


def _failure_status(status: str, error: Any = "") -> str:
    status = str(status or "").upper()
    message = str(error or "").lower()
    if status == "OK":
        return "SUCCESS"
    if status in {"BLOCKED", "TIMEOUT", "PARSE_ERROR", "NETWORK_ERROR", "EMPTY", "SUCCESS"}:
        return status
    if "captcha" in message or "robot check" in message or "access denied" in message or "human verification" in message:
        return "BLOCKED"
    if "timeout" in message or "timed out" in message:
        return "TIMEOUT"
    if any(term in message for term in ("connection reset", "connection error", "dns", "name resolution", "network", "navigation failed")):
        return "NETWORK_ERROR"
    return "PARSE_ERROR" if status in {"FAILED", "ERROR", "OK"} else "EMPTY"


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
        self._myntra = MyntraScraper(headless=headless, max_retries=max_retries)
        self._meesho = MeeshoScraper(headless=headless, max_retries=max_retries)

    async def _run_flipkart(
        self,
        product_name: str,
        category: Optional[str],
        product_details: Optional[str],
        max_products: int,
    ) -> Dict[str, Any]:
        """Execute Flipkart scrape with error isolation and pipeline filtering."""
        try:
            logger.info("[SCOUT] Running Flipkart scraper")
            res = await self._flipkart.scrape_flipkart_with_retry(
                product_name=product_name,
                category=category,
                product_details=product_details,
                max_products=min(max_products, HARD_MAX_PRODUCTS),
            )
            if not isinstance(res, dict):
                res = {"platform": "Flipkart", "status": "SUCCESS" if res else "EMPTY", "products": res or []}

            status = _failure_status(res.get("status", "EMPTY"), res.get("error"))
            if status in ("BLOCKED", "TIMEOUT", "PARSE_ERROR"):
                self._flipkart.log_diagnostic("Flipkart", "normalization", final_status=status,
                                               reason_category=self._flipkart.result_reason_category(status),
                                               normalized_product_count=0)
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
            self._flipkart.log_diagnostic("Flipkart", "normalization", final_status=final_status,
                                           reason_category=self._flipkart.result_reason_category(final_status),
                                           normalized_product_count=len(valid_prods))
            return {
                "platform": "Flipkart",
                "status": final_status,
                "products": valid_prods,
                "error": None if valid_prods else "No relevant products found",
            }
        except Exception as e:
            self._flipkart.log_diagnostic("Flipkart", "normalization", final_status="PARSE_ERROR",
                                           reason_category="scraper_failure", exception=e,
                                           normalized_product_count=0)
            logger.error("[SCOUT] Flipkart runner error: exception_class=%s", type(e).__name__)
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
            logger.info("[SCOUT] Running Amazon scraper")
            res = await self._amazon.scrape_amazon_with_retry(
                product_name=product_name,
                category=category,
                product_details=product_details,
                max_products=min(max_products, HARD_MAX_PRODUCTS),
            )
            if not isinstance(res, dict):
                res = {"platform": "Amazon", "status": "SUCCESS" if res else "EMPTY", "products": res or []}

            status = _failure_status(res.get("status", "EMPTY"), res.get("error"))
            if status in ("BLOCKED", "TIMEOUT", "PARSE_ERROR"):
                self._amazon.log_diagnostic("Amazon", "normalization", final_status=status,
                                            reason_category=self._amazon.result_reason_category(status),
                                            normalized_product_count=0)
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
            self._amazon.log_diagnostic("Amazon", "normalization", final_status=final_status,
                                        reason_category=self._amazon.result_reason_category(final_status),
                                        normalized_product_count=len(valid_prods))
            return {
                "platform": "Amazon",
                "status": final_status,
                "products": valid_prods,
                "error": None if valid_prods else "No relevant products found",
            }
        except Exception as e:
            self._amazon.log_diagnostic("Amazon", "normalization", final_status="PARSE_ERROR",
                                        reason_category="scraper_failure", exception=e,
                                        normalized_product_count=0)
            logger.error("[SCOUT] Amazon runner error: exception_class=%s", type(e).__name__)
            return {"platform": "Amazon", "status": "PARSE_ERROR", "products": [], "error": str(e)}

    async def scrape_sequential(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
        platforms: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Sequential execution with independent failure handling.
        Returns per-platform outcomes without suppressing later marketplaces.
        """
        limit = min(max_products or HARD_MAX_PRODUCTS, HARD_MAX_PRODUCTS)

        # Execute independently and sequentially so one site's challenge or
        # network failure cannot prevent collection from the remaining sites.
        async def safely_run(platform, runner):
            scraper = {"Amazon": self._amazon, "Myntra": self._myntra,
                       "Meesho": self._meesho, "Flipkart": self._flipkart}[platform]
            try:
                platform_timeout = AMAZON_PLATFORM_TIMEOUT_SECONDS if platform == "Amazon" else PLATFORM_TIMEOUT_SECONDS
                return await asyncio.wait_for(
                    runner(product_name, category, product_details, limit),
                    timeout=platform_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("[SCOUT] %s exceeded its bounded marketplace timeout", platform)
                scraper.log_diagnostic(platform, "final", final_status="TIMEOUT",
                                       reason_category="timeout", exception=asyncio.TimeoutError(),
                                       normalized_product_count=0)
                return {"platform": platform, "status": "TIMEOUT", "products": [], "error": "Marketplace request timed out"}
            except Exception as exc:
                status = _failure_status("FAILED", exc)
                scraper.log_diagnostic(platform, "final", final_status=status,
                                       reason_category=scraper.result_reason_category(status), exception=exc,
                                       normalized_product_count=0)
                return {"platform": platform, "status": status, "products": [], "error": str(exc)}

        runners = {
            "Myntra": self._run_myntra,
            "Meesho": self._run_meesho,
            "Amazon": self._run_amazon,
            "Flipkart": self._run_flipkart,
        }
        selected = list(runners) if platforms is None else [
            name for name in runners if any(str(requested).casefold() == name.casefold() for requested in platforms)
        ]
        results = {}
        for platform in selected:
            results[platform] = await safely_run(platform, runners[platform])
            # Honor the user's requested fallback order and stop once a
            # marketplace supplies normalized products. If callers explicitly
            # select multiple platforms for comparison, collect each selection.
            if platforms is None and results[platform].get("products"):
                break
        return results

    async def _run_additional_platform(self, scraper, platform, method_name, product_name, category, product_details, limit):
        try:
            result = await getattr(scraper, method_name)(product_name, category, product_details, limit)
            status = _failure_status(result.get("status", "EMPTY"), result.get("error"))
            if status not in {"SUCCESS", "BLOCKED", "EMPTY", "PARSE_ERROR", "NETWORK_ERROR", "TIMEOUT"}:
                status = "PARSE_ERROR"
            products = result.get("products", []) if status == "SUCCESS" else []
            if status == "SUCCESS":
                products, _ = process_platform_pipeline(platform, product_name, category, products, limit)
                status = "SUCCESS" if products else "EMPTY"
            scraper.log_diagnostic(platform, "normalization", final_status=status,
                                   reason_category=scraper.result_reason_category(status),
                                   normalized_product_count=len(products))
            return {"platform": platform, "status": status, "products": products[:limit], "error": result.get("error")}
        except Exception as exc:
            scraper.log_diagnostic(platform, "normalization", final_status="PARSE_ERROR",
                                   reason_category="scraper_failure", exception=exc,
                                   normalized_product_count=0)
            logger.warning("[SCOUT] %s failed: exception_class=%s", platform, type(exc).__name__)
            return {"platform": platform, "status": _failure_status("FAILED", exc), "products": [], "error": str(exc)}

    async def _run_myntra(self, product_name, category=None, product_details=None, max_products=10):
        return await self._run_additional_platform(self._myntra, "Myntra", "scrape_myntra_with_retry", product_name, category, product_details, max_products)

    async def _run_meesho(self, product_name, category=None, product_details=None, max_products=10):
        return await self._run_additional_platform(self._meesho, "Meesho", "scrape_meesho_with_retry", product_name, category, product_details, max_products)

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
        seq_res = await self.scrape_sequential(product_name, category, product_details, limit, platforms)

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
        seq_res = await self.scrape_sequential(product_name, category, product_details, limit, platforms)

        all_products = []
        platform_statuses = {}
        blocked = []
        failed = []

        for plat_name, res in seq_res.items():
            status = _failure_status(res.get("status", "EMPTY"), res.get("error"))
            platform_statuses[plat_name] = status
            if status == "BLOCKED":
                blocked.append(plat_name)
            elif status not in ("SUCCESS", "EMPTY"):
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
