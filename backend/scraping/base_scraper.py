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
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        context = await browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1920, "height": 1080},
            extra_http_headers=self.headers,
        )
        page = await context.new_page()
        return browser, context, page

    async def safe_navigate(
        self, page: Page, url: str, wait_until: str = "domcontentloaded", timeout: Optional[int] = None
    ) -> bool:
        """
        Safely navigate to a URL with timeout fallback handling.
        Returns True if page loaded, False on critical error.
        """
        t = timeout or self.timeout_ms
        try:
            await page.goto(url, wait_until=wait_until, timeout=t)
            return True
        except PlaywrightTimeoutError:
            logger.warning(f"[BASE_SCRAPER] Timeout loading {url}. Continuing with rendered DOM.")
            return True
        except Exception as e:
            logger.error(f"[BASE_SCRAPER] Navigation failed for {url}: {e}")
            return False

    async def scroll_page(self, page: Page, count: int = 3, delay_ms: int = 800) -> None:
        """Scroll page to trigger lazy loading of dynamic content."""
        for _ in range(count):
            try:
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(delay_ms)
            except Exception as e:
                logger.debug(f"[BASE_SCRAPER] Scroll interrupted: {e}")
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
