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
    clean_name = re.sub(r"\b\d+\s*(?:ml|gm|g|kg|l|oz|pack)\b", "", product_name, flags=re.IGNORECASE)
    clean_name = re.sub(r"['’]", "", clean_name).strip()
    words = [w for w in clean_name.split() if len(w) >= 3 and w.lower() not in {"and", "for", "the", "with", "all", "our"}]
    focused = " ".join(words[:4]) if words else product_name.strip()
    logger.info(f"[FLIPKART_SCRAPER] Focused search query built: '{focused}'")
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

        logger.info(f"[FLIPKART_SCRAPING_STARTED] Navigating to search URL: {search_url}")
        nav_ok = await self.safe_navigate(page, search_url, wait_until="domcontentloaded")

        if not nav_ok:
            logger.error("[FLIPKART_SCRAPING_FAILED] reason=page_navigation_error")
            return [], False

        await self._dismiss_login_modal(page)

        # Scroll to trigger lazy loading of product cards
        await self.scroll_page(page, count=3, delay_ms=900)

        current_url = page.url
        html = await page.content()
        if not html or len(html) < 500:
            logger.error("[FLIPKART_SCRAPING_FAILED] reason=empty_page_content")
            return [], False

        if _is_flipkart_blocked(html, current_url):
            logger.warning(
                f"[FLIPKART_SCRAPING_BLOCKED] Bot challenge detected at URL: {current_url[:80]}"
            )
            return [], True

        logger.info("[PAGE_RENDERED] Search page content captured successfully.")
        candidates = parse_search_page(html, query=query)
        logger.info(f"[DATA_EXTRACTED] Search page candidates discovered: {len(candidates)}")
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

        logger.info(f"[FLIPKART_SCRAPER] Detail page {idx}/{total}: {cand_url[:80]}")

        try:
            await page.goto(cand_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)  # Allow JS/React spec tables to hydrate
            detail_html = await page.content()
        except Exception as e:
            logger.warning(
                f"[FLIPKART_SCRAPER] Detail page load failed for {cand_url[:60]}: {e}. "
                "Using search card fallback."
            )
            detail_html = ""

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
            "availability": detailed.get("availability", cand.get("availability", True)),
            "product_details": detailed.get("product_details") or cand.get("product_details", []),
            "scraped_at": scraped_at,
            "ranking": cand.get("ranking", idx),
        }

        if merged["product_title"]:
            logger.info(
                f"[DATA_EXTRACTED] Product {idx}: '{merged['product_title'][:40]}' "
                f"price={merged['price']} rating={merged['rating']}"
            )

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

        logger.info(
            f"[FLIPKART_SCRAPING_STARTED] product='{product_name}' "
            f"query='{query}' max_products={limit}"
        )

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
                            "status": "EMPTY",
                            "products": [],
                            "error": "Empty search results",
                        }

                    selected = candidates[:limit]
                    logger.info(
                        f"[FLIPKART_SCRAPER] Processing {len(selected)} candidates "
                        f"(of {len(candidates)} found)"
                    )

                    # Phase 2: Detail pages
                    for idx, cand in enumerate(selected, 1):
                        product = await self._fetch_detail_page(page, cand, idx, len(selected))
                        if product and product.get("product_title"):
                            results.append(product)

                finally:
                    await self.close_session(browser, context)

        except PlaywrightTimeoutError as te:
            logger.error(f"[FLIPKART_SCRAPING_FAILED] Timeout: {te}")
            return {
                "platform": "Flipkart",
                "status": "TIMEOUT",
                "products": [],
                "error": str(te),
            }
        except Exception as e:
            logger.error(
                f"[FLIPKART_SCRAPING_FAILED] Unexpected error during scraping: {e}",
                exc_info=True,
            )
            return {
                "platform": "Flipkart",
                "status": "PARSE_ERROR",
                "products": [],
                "error": str(e),
            }

        logger.info(
            f"[FLIPKART_SCRAPING_COMPLETED] product='{product_name}' "
            f"results_collected={len(results)}"
        )
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

                status = res.get("status")
                if status in ("SUCCESS", "BLOCKED"):
                    return res

                if attempt_num < self.max_retries:
                    import asyncio
                    await asyncio.sleep(2 ** attempt_num)
                else:
                    return res
            except Exception as e:
                if attempt_num >= self.max_retries:
                    return {
                        "platform": "Flipkart",
                        "status": "PARSE_ERROR",
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
