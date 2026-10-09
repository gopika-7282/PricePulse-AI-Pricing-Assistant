"""
base_scraper.py
===============
Base scraper architecture using Playwright and Tenacity retries.

Responsibilities:
- Playwright Chromium browser lifecycle management (launch, context, cleanup)
- Dynamic page navigation and scrolling for lazy loading
- Timeout management and anti-bot evasions
- Tenacity retry wrapper for temporary network/timeout failures
- Structured error handling preventing FastAPI crashes
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
import re
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError
)
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 25000


class BaseScraper:
    """
    Abstract base class providing Playwright browser lifecycle management,
    anti-bot evasions, and resilient dynamic page fetching with Tenacity retries.
    """

    def __init__(self, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS, max_retries: int = 3):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.last_navigation_status = None
        self.last_navigation_error = None
        self.search_result_status = "PARSE_ERROR"
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )
        self.headers = {
            "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        }

    async def create_browser_session(self, p: Any) -> Tuple[Browser, BrowserContext, Page]:
        """
        Launch Playwright Chromium browser instance with anti-bot evasion arguments.
        Returns (browser, context, page).
        """
        browser = await p.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1920, "height": 1080},
            extra_http_headers=self.headers,
        )
        page = await context.new_page()
        return browser, context, page

    @staticmethod
    def _safe_url_location(url: Optional[str]) -> Tuple[str, str]:
        """Return only a URL's host and path; omit credentials and query data."""
        try:
            parsed = urlsplit(str(url or ""))
            host = parsed.hostname or "unknown"
            path = parsed.path or "/"
            return (re.sub(r"[\r\n\t]", "", host)[:120],
                    re.sub(r"[\r\n\t]", "", path)[:240])
        except (TypeError, ValueError):
            return "unknown", "/"

    def log_diagnostic(
        self,
        platform: str,
        stage: str,
        *,
        requested_url: Optional[str] = None,
        final_url: Optional[str] = None,
        navigation: Optional[str] = None,
        exception: Optional[BaseException] = None,
        challenge: Optional[bool] = None,
        challenge_reason: Optional[str] = None,
        product_card_count: Optional[int] = None,
        normalized_product_count: Optional[int] = None,
        final_status: Optional[str] = None,
        reason_category: Optional[str] = None,
    ) -> None:
        requested_host, _ = self._safe_url_location(requested_url)
        final_host, final_path = self._safe_url_location(final_url)
        logger.info(
            "[SCRAPER_DIAGNOSTIC] platform=%s stage=%s requested_host=%s final_host=%s final_path=%s "
            "navigation=%s exception_class=%s challenge=%s challenge_reason=%s "
            "product_card_count=%s normalized_product_count=%s final_status=%s reason_category=%s",
            platform, stage, requested_host, final_host, final_path,
            navigation or "not_applicable", type(exception).__name__ if exception else "none",
            str(challenge).lower() if challenge is not None else "not_checked",
            challenge_reason or "none",
            product_card_count if product_card_count is not None else "not_measured",
            normalized_product_count if normalized_product_count is not None else "not_measured",
            final_status or "not_final", reason_category or "none",
        )

    @staticmethod
    def challenge_reason_category(
        detected: bool, html: str, url: str, url_markers=(), html_markers=()
    ) -> str:
        if not detected:
            return "not_detected"
        if any(marker.casefold() in (url or "").casefold() for marker in url_markers):
            return "url_pattern"
        if any(marker.casefold() in (html or "").casefold() for marker in html_markers):
            return "html_signal"
        return "detector_match"

    async def safe_navigate(
        self, page: Page, url: str, wait_until: str = "domcontentloaded", timeout: Optional[int] = None,
        *, platform: str = "unknown", stage: str = "search",
    ) -> bool:
        """
        Safely navigate to a URL with timeout fallback handling.
        Returns True if page loaded, False on critical error.
        """
        t = timeout or self.timeout_ms
        self.last_navigation_status = None
        self.last_navigation_error = None
        try:
            await page.goto(url, wait_until=wait_until, timeout=t)
            self.log_diagnostic(platform, stage, requested_url=url, final_url=getattr(page, "url", None),
                                navigation="success")
            return True
        except PlaywrightTimeoutError as e:
            self.last_navigation_status = "TIMEOUT"
            self.last_navigation_error = "Navigation timed out"
            self.log_diagnostic(platform, stage, requested_url=url, final_url=getattr(page, "url", None),
                                navigation="timeout_fallback", exception=e, reason_category="timeout")
            return True
        except Exception as e:
            self.last_navigation_status = "NETWORK_ERROR"
            self.last_navigation_error = str(e)
            self.log_diagnostic(platform, stage, requested_url=url, final_url=getattr(page, "url", None),
                                navigation="failure", exception=e, reason_category="network_failure")
            return False

    @staticmethod
    def result_reason_category(status: str) -> str:
        return {
            "SUCCESS": "success", "OK": "success", "BLOCKED": "challenge_detected",
            "TIMEOUT": "timeout", "NETWORK_ERROR": "network_failure",
            "PARSE_ERROR": "parser_failure", "EMPTY": "no_products",
            "FAILED": "scraper_failure",
        }.get(str(status or "").upper(), "unknown")

    @staticmethod
    def classify_empty_search(html: str) -> str:
        text = (html or "").lower()
        if any(marker in text for marker in ("no results found", "no products found", "did not match any products", "try a different search")):
            return "EMPTY"
        return "PARSE_ERROR"

    def search_failure_message(self) -> str:
        if self.search_result_status == "EMPTY":
            return "No matching products were returned by the marketplace."
        if self.search_result_status == "TIMEOUT":
            detail = f" Details: {self.last_navigation_error}" if self.last_navigation_error else ""
            return f"Marketplace navigation timed out.{detail}"
        if self.search_result_status == "NETWORK_ERROR":
            detail = f" Details: {self.last_navigation_error}" if self.last_navigation_error else ""
            return f"Marketplace connection failed during navigation.{detail}"
        return "The search page loaded, but its product cards could not be parsed."

    @staticmethod
    def classify_status(status: str, error: Any = "") -> str:
        value = str(status or "").upper()
        message = str(error or "").lower()
        if value == "OK": return "SUCCESS"
        if value in {"SUCCESS", "BLOCKED", "EMPTY", "PARSE_ERROR", "NETWORK_ERROR", "TIMEOUT"}: return value
        if any(term in message for term in ("captcha", "robot check", "access denied", "verify you are human", "security challenge")): return "BLOCKED"
        if "timeout" in message or "timed out" in message: return "TIMEOUT"
        if any(term in message for term in ("connection reset", "connection error", "dns", "name resolution", "network", "navigation failed")): return "NETWORK_ERROR"
        return "PARSE_ERROR"

    async def scroll_page(self, page: Page, count: int = 3, delay_ms: int = 800) -> None:
        """Scroll page to trigger lazy loading of dynamic content."""
        for _ in range(count):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(delay_ms)
            except Exception as e:
                logger.debug("[BASE_SCRAPER] Scroll interrupted: exception_class=%s", type(e).__name__)
                break

    async def close_session(self, browser: Optional[Browser], context: Optional[BrowserContext] = None) -> None:
        """Clean up Playwright browser context and resources safely."""
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
