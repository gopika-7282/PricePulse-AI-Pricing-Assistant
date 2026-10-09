"""
flipkart_scraper.py
===================
Flipkart-specific dynamic scraper built on top of BaseScraper.

Responsibilities:
- Inherits Playwright browser lifecycle from BaseScraper
- Generates dynamic search queries from ProductCatalog fields
- Navigates Flipkart search and product detail pages
- Delegates HTML parsing to parser.py
- Retries temporary failures via Tenacity
- Emits all required structured log tags

Required log tags:
  FLIPKART_SCRAPING_STARTED
  PAGE_RENDERED
  DATA_EXTRACTED
  FLIPKART_SCRAPING_FAILED
  FLIPKART_SCRAPING_COMPLETED
"""
import re
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)

from scraping.base_scraper import BaseScraper
from scraping.parser import parse_search_page, parse_detail_page, clean_flipkart_url
from scraping.query_builder import focused_product_query

logger = logging.getLogger(__name__)

# Maximum product detail pages to visit per search - hard capped at 10
DEFAULT_MAX_PRODUCTS = 10
HARD_MAX_PRODUCTS = 10

FLIPKART_BLOCK_SIGNALS = [
    "enter the characters you see below",
    "robot check",
    "unusual traffic",
    "access denied",
    "verify you are human",
    "please enter the captcha",
    "403 forbidden",
    "challenge",
    "automated access",
    "blocked",
]


def _is_flipkart_blocked(html: str, page_url: str = "") -> bool:
    """Detect Flipkart CAPTCHA and bot-block states."""
    if not html:
        return False
    lower_html = html.lower()
    for signal in FLIPKART_BLOCK_SIGNALS:
        if signal in lower_html:
            return True
    return False


def build_flipkart_query(
    product_name: str,
    category: Optional[str] = None,
    product_details: Optional[str] = None,
) -> str:
    """
    Focused query builder: 2-3 focused terms from product name and type.
    Avoids long description dumping.
    """
    focused = focused_product_query(product_name, category, product_details)
    logger.info("[FLIPKART_SCRAPER] Focused search query built")
    return focused


class FlipkartScraper(BaseScraper):
    """
    Flipkart-specific scraper extending BaseScraper.
    Handles Flipkart search navigation, lazy loading, and product detail extraction.
    """

    def __init__(self, headless: bool = True, timeout_ms: Optional[int] = None, max_retries: int = 3):
        super().__init__(
            headless=headless,
            timeout_ms=timeout_ms or 25000,
            max_retries=max_retries,
        )

    async def _dismiss_login_modal(self, page: Any) -> None:
        """Silently dismiss Flipkart login modal if it appears."""
        try:
            await page.click(
                "button._2KpZ6l._2doB4z, span._30XB9F, button[class*='close'], div[role='dialog'] button",
                timeout=2500,
            )
            logger.debug("[FLIPKART_SCRAPER] Login modal dismissed.")
        except Exception:
            pass  # Modal not present — expected in most cases

    async def _fetch_search_candidates(
        self, page: Any, query: str
    ) -> List[Dict[str, Any]]:
        """
        Navigate to Flipkart search, scroll for lazy loading, parse candidate cards.
        Returns raw candidate list from parser.
        """
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.flipkart.com/search?q={encoded_query}"

        logger.info("[FLIPKART_SCRAPING_STARTED] platform=Flipkart stage=search")
        self.search_result_status = "PARSE_ERROR"
        nav_ok = await self.safe_navigate(page, search_url, wait_until="domcontentloaded", platform="Flipkart", stage="search")

        if not nav_ok:
            self.search_result_status = self.last_navigation_status or "NETWORK_ERROR"
            self.log_diagnostic("Flipkart", "search", requested_url=search_url,
                                final_url=getattr(page, "url", None), navigation="failure",
                                challenge=None, challenge_reason="not_checked_navigation_failure",
                                product_card_count=0, normalized_product_count=0,
                                reason_category=self.result_reason_category(self.search_result_status))
            logger.error("[FLIPKART_SCRAPING_FAILED] reason=page_navigation_error")
            return [], False

        await self._dismiss_login_modal(page)

        # Scroll to trigger lazy loading of product cards
        await self.scroll_page(page, count=3, delay_ms=900)

        current_url = page.url
        html = await page.content()
        if not html or len(html) < 500:
            self.search_result_status = self.last_navigation_status or "PARSE_ERROR"
            self.log_diagnostic("Flipkart", "search", requested_url=search_url, final_url=current_url,
                                challenge=None, challenge_reason="not_checked_short_or_empty_page",
                                product_card_count=0, normalized_product_count=0,
                                reason_category="empty_page_content")
            logger.error("[FLIPKART_SCRAPING_FAILED] reason=empty_page_content")
            return [], False

        challenge_detected = _is_flipkart_blocked(html, current_url)
        self.log_diagnostic("Flipkart", "search", requested_url=search_url, final_url=current_url,
                            challenge=challenge_detected,
                            challenge_reason=self.challenge_reason_category(
                                challenge_detected, html, current_url,
                                html_markers=FLIPKART_BLOCK_SIGNALS,
                            ))
        if challenge_detected:
            return [], True

        logger.info("[PAGE_RENDERED] Search page content captured successfully.")
        candidates = parse_search_page(html, query=query)
        if not candidates:
            self.search_result_status = self.last_navigation_status or self.classify_empty_search(html)
        self.log_diagnostic("Flipkart", "search_parse", requested_url=search_url, final_url=current_url,
                            product_card_count=len(candidates))
        return candidates, False

    async def _fetch_detail_page(
        self, page: Any, cand: Dict[str, Any], idx: int, total: int
    ) -> Dict[str, Any]:
        """
        Fetch and parse a single Flipkart product detail page.
        Returns merged product dict (detail + search card fallback).
        """
        cand_url = clean_flipkart_url(cand.get("product_url", ""))
        if not cand_url:
            logger.warning(f"[FLIPKART_SCRAPER] Skipping candidate {idx}/{total}: empty URL.")
            return {}

        logger.info("[FLIPKART_SCRAPER] platform=Flipkart stage=detail candidate=%d/%d", idx, total)

        try:
            await page.goto(cand_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            self.log_diagnostic("Flipkart", "detail", requested_url=cand_url,
                                final_url=getattr(page, "url", None), navigation="failure",
                                exception=e, reason_category="detail_navigation_failure")
            logger.warning("[FLIPKART_SCRAPER] Detail page load failed; using search card fallback. exception_class=%s",
                           type(e).__name__)
            detail_html = ""
        else:
            self.log_diagnostic("Flipkart", "detail", requested_url=cand_url,
                                final_url=getattr(page, "url", None), navigation="success")
            try:
                await page.wait_for_timeout(3000)  # Allow JS/React spec tables to hydrate
                detail_html = await page.content()
            except Exception as e:
                self.log_diagnostic("Flipkart", "detail_capture", requested_url=cand_url,
                                    final_url=getattr(page, "url", None), navigation="success",
                                    exception=e, reason_category="page_capture_failure")
                detail_html = ""

        self.log_diagnostic("Flipkart", "detail", requested_url=cand_url,
                            final_url=getattr(page, "url", None), challenge=None,
                            challenge_reason="not_checked_no_flipkart_detail_detector")

        if detail_html and len(detail_html) > 500:
            logger.info(f"[PAGE_RENDERED] Detail page rendered for candidate {idx}/{total}.")
            detailed = parse_detail_page(detail_html, cand_url)
        else:
            detailed = {}

        scraped_at = datetime.now(timezone.utc).isoformat()

        merged = {
            "platform": "Flipkart",
            "product_title": detailed.get("product_name") or cand.get("product_name", ""),
            "product_url": cand_url,
            "price": detailed.get("price") or cand.get("price") or 0.0,
            "rating": (
                detailed.get("rating")
                if detailed.get("rating") is not None
                else cand.get("rating")
            ),
            "availability": (detailed.get("availability") if detailed.get("availability") is not None
                             else cand.get("availability")),
            "product_details": detailed.get("product_details") or cand.get("product_details", []),
            "scraped_at": scraped_at,
            "ranking": cand.get("ranking", idx),
        }

        self.log_diagnostic("Flipkart", "detail_parse", requested_url=cand_url,
                            final_url=getattr(page, "url", None),
                            normalized_product_count=1 if merged["product_title"] else 0,
                            reason_category="parsed" if merged["product_title"] else "missing_product_title")

        return merged

    async def scrape_flipkart(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Any:
        """
        Main scrape entry point.
        Builds query → fetches search page → fetches detail pages → returns raw product list or block dict.
        Wraps entire flow with structured failure handling.
        """
        limit = min(max_products or DEFAULT_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
        query = build_flipkart_query(product_name, category, product_details)

        logger.info("[FLIPKART_SCRAPING_STARTED] platform=Flipkart stage=scrape max_products=%d", limit)

        results: List[Dict[str, Any]] = []

        try:
            async with async_playwright() as p:
                browser, context, page = await self.create_browser_session(p)

                try:
                    # Phase 1: Search page
                    candidates, is_blocked = await self._fetch_search_candidates(page, query)

                    if is_blocked:
                        return {
                            "platform": "Flipkart",
                            "status": "BLOCKED",
                            "products": [],
                            "error": "Automation challenge detected on search page",
                        }

                    if not candidates:
                        logger.warning(
                            f"[FLIPKART_SCRAPING_FAILED] reason=empty_search_results query='{query}'"
                        )
                        return {
                            "platform": "Flipkart",
                            "status": self.search_result_status,
                            "products": [],
                            "error": self.search_failure_message(),
                        }

                    selected = candidates[:limit]
                    self.log_diagnostic("Flipkart", "candidate_selection", product_card_count=len(candidates),
                                        reason_category="selection_complete")

                    # Phase 2: Detail pages
                    for idx, cand in enumerate(selected, 1):
                        product = await self._fetch_detail_page(page, cand, idx, len(selected))
                        if product and product.get("product_title"):
                            results.append(product)

                finally:
                    await self.close_session(browser, context)

        except PlaywrightTimeoutError as te:
            logger.error("[FLIPKART_SCRAPING_FAILED] platform=Flipkart reason_category=timeout exception_class=%s",
                         type(te).__name__)
            return {
                "platform": "Flipkart",
                "status": "TIMEOUT",
                "products": [],
                "error": str(te),
            }
        except Exception as e:
            logger.error("[FLIPKART_SCRAPING_FAILED] platform=Flipkart reason_category=scraper_failure exception_class=%s",
                         type(e).__name__)
            return {
                "platform": "Flipkart",
                "status": "PARSE_ERROR",
                "products": [],
                "error": str(e),
            }

        self.log_diagnostic("Flipkart", "scrape", normalized_product_count=len(results),
                            final_status="SUCCESS" if results else "EMPTY",
                            reason_category="success" if results else "no_products")
        return {
            "platform": "Flipkart",
            "status": "SUCCESS" if results else "EMPTY",
            "products": results,
            "error": None if results else "No valid products extracted",
        }

    async def scrape_flipkart_with_retry(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Tenacity-wrapped version of scrape_flipkart.
        Retries up to max_retries times on transient failures with exponential backoff.
        Does NOT retry if platform returns BLOCKED.
        """
        for attempt_num in range(1, self.max_retries + 1):
            try:
                res = await self.scrape_flipkart(product_name, category, product_details, max_products)
                if not isinstance(res, dict):
                    res = {"platform": "Flipkart", "status": "SUCCESS" if res else "EMPTY", "products": res or []}

                status = self.classify_status(res.get("status"), res.get("error"))
                res = {**res, "status": status}
                self.log_diagnostic("Flipkart", "attempt_result", final_status=status,
                                    reason_category=self.result_reason_category(status),
                                    normalized_product_count=len(res.get("products") or []))
                if status in {"SUCCESS", "BLOCKED", "EMPTY", "PARSE_ERROR"}:
                    return res

                if status not in {"NETWORK_ERROR", "TIMEOUT"}:
                    return res
                if attempt_num < self.max_retries:
                    import asyncio
                    await asyncio.sleep(2 ** attempt_num)
                else:
                    return res
            except Exception as e:
                status = self.classify_status("FAILED", e)
                self.log_diagnostic("Flipkart", "attempt_result", final_status=status,
                                    reason_category=self.result_reason_category(status), exception=e)
                if status not in {"NETWORK_ERROR", "TIMEOUT"} or attempt_num >= self.max_retries:
                    return {
                        "platform": "Flipkart",
                        "status": status,
                        "products": [],
                        "error": str(e),
                    }
                import asyncio
                await asyncio.sleep(2 ** attempt_num)

        return {
            "platform": "Flipkart",
            "status": "EMPTY",
            "products": [],
            "error": "All attempts exhausted",
        }
