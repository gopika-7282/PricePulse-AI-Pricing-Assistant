"""
myntra_scraper.py
=================
Myntra-specific dynamic scraper built on top of BaseScraper.

Responsibilities:
- Inherits Playwright browser lifecycle from BaseScraper
- Generates dynamic search queries from ProductCatalog fields
- Navigates Myntra search and product detail pages
- Resilient multi-layer HTML parsing (JSON-LD -> semantic -> CSS fallbacks)
- Login wall / bot-block detection with structured failure result
- Retries temporary failures; does NOT retry login/block pages
- Emits all required structured log tags

Required log tags:
  MYNTRA_SCRAPING_STARTED
  MYNTRA_PAGE_RENDERED
  MYNTRA_DATA_EXTRACTED
  MYNTRA_SCRAPING_BLOCKED
  MYNTRA_SCRAPING_FAILED
  MYNTRA_SCRAPING_COMPLETED
"""

import asyncio
import json
import logging
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from scraping.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

DEFAULT_MAX_PRODUCTS = 10
HARD_MAX_PRODUCTS = 10

# Signals that Myntra is blocking or presenting a login wall
MYNTRA_BLOCK_SIGNALS = [
    "verify you are human",
    "access denied",
    "unusual traffic",
    "please login",
    "sign in to continue",
    "robot check",
    "captcha",
    "403 forbidden",
    "challenge",
]

MYNTRA_LOGIN_WALL_SIGNALS = [
    "login to continue",
    "sign in to see",
    "please sign in",
]


def build_myntra_query(
    product_name: str,
    category: Optional[str] = None,
    product_details: Optional[str] = None,
) -> str:
    """
    Dynamically compose a Myntra search query from ProductCatalog fields.

    Strategy:
      1. Start with the product name (mandatory).
      2. Append up to 3 meaningful tokens from category.
      3. Append the first non-trivial word cluster from product_details.
    Avoids hardcoded synonym mapping.
    """
    parts = [product_name.strip()]

    if category and category.strip():
        cat_tokens = category.strip().split()[:3]
        cat_str = " ".join(cat_tokens).lower()
        if cat_str and cat_str.lower() not in product_name.lower():
            parts.append(cat_str)

    if product_details and product_details.strip():
        detail_tokens = product_details.strip().split()
        detail_hint = " ".join(t for t in detail_tokens[:6] if len(t) > 2)
        if detail_hint and detail_hint.lower() not in " ".join(parts).lower():
            parts.append(detail_hint)

    query = " ".join(parts)
    logger.info(f"[MYNTRA_SCRAPER] Dynamic search query built: '{query}'")
    return query


def _is_myntra_blocked(html: str, page_url: str = "") -> bool:
    """
    Detect Myntra login walls, CAPTCHA, and bot-block states.
    Returns True if the page is not a real search result page.
    Checks URL patterns first (reliable even without html), then body signals.
    """
    # URL-based detection first (reliable even when html is empty)
    if page_url and "/login" in page_url and "myntra.com" in page_url:
        return True
    if not html:
        return False
    lower_html = html.lower()
    all_signals = MYNTRA_BLOCK_SIGNALS + MYNTRA_LOGIN_WALL_SIGNALS
    for signal in all_signals:
        if signal in lower_html:
            return True
    return False


def _parse_numeric_price(val: Any) -> Optional[float]:
    """Parse positive numeric price from raw text."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None
    val_str = str(val).replace(",", "").strip()
    match = re.search(r'(?:rs\.?|inr)?\s*(\d+(?:\.\d{1,2})?)', val_str, re.IGNORECASE)
    if match:
        try:
            p = float(match.group(1))
            return p if p > 0 else None
        except (ValueError, TypeError):
            pass
    return None


def _parse_numeric_rating(val: Any) -> Optional[float]:
    """Parse and validate numeric rating [0.0, 5.0]."""
    if val is None:
        return None
    val_str = str(val).strip()
    match = re.search(r'([0-5](?:\.\d+)?)', val_str)
    if match:
        try:
            rating = float(match.group(1))
            if 0.0 <= rating <= 5.0:
                return round(rating, 2)
        except (ValueError, TypeError):
            pass
    return None


def _parse_myntra_search_page(html: str) -> List[Dict[str, Any]]:
    """
    Parse rendered Myntra search results HTML.
    Multi-layer approach:
      1. JSON-LD Product schema (if present)
      2. Semantic product card containers: li[data-index] or .product-base
      3. CSS fallbacks
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, Any]] = []
    seen_urls: set = set()

    # Layer 1: JSON-LD (Myntra occasionally embeds structured data)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string or len(results) >= HARD_MAX_PRODUCTS:
                break
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "Product":
                    name = item.get("name", "").strip()
                    url = item.get("url", "").strip()
                    if not name or not url or url in seen_urls:
                        continue
                    offers = item.get("offers", {})
                    price = None
                    if isinstance(offers, dict) and offers.get("price"):
                        price = _parse_numeric_price(offers["price"])
                    agg = item.get("aggregateRating", {})
                    rating = None
                    if isinstance(agg, dict):
                        rating = _parse_numeric_rating(agg.get("ratingValue"))
                    seen_urls.add(url)
                    results.append({
                        "platform": "Myntra",
                        "product_title": name,
                        "product_url": url if url.startswith("http") else f"https://www.myntra.com{url}",
                        "price": price or 0.0,
                        "rating": rating,
                        "availability": True,
                        "product_details": [],
                        "ranking": len(results) + 1,
                    })
        except Exception:
            pass

    if results:
        logger.info(f"[MYNTRA_SCRAPER] JSON-LD yielded {len(results)} candidates")
        return results

    # Layer 2: Semantic HTML product cards
    # Myntra uses <li class="product-base"> or similar list structures
    product_items = soup.find_all(
        "li",
        class_=lambda x: x and any(k in x for k in ["product-base", "product-productMetaInfo", "results-base"])
    )
    if not product_items:
        # Fallback: any li with a product anchor
        product_items = soup.find_all(
            "li",
            attrs={"data-index": True}
        )
    if not product_items:
        # Broader fallback: anchor-based discovery
        product_anchors = soup.find_all(
            "a",
            href=lambda h: h and "/" in h and "myntra.com" not in (h or "")
        )
        product_items = list({a.parent for a in product_anchors if a.parent})

    logger.info(f"[MYNTRA_SCRAPER] Found {len(product_items)} semantic product containers")

    for item in product_items:
        try:
            # Product link
            a_tag = item.find("a", href=True)
            if not a_tag or not a_tag.get("href"):
                continue
            href = a_tag.get("href", "")
            if href.startswith("/"):
                href = f"https://www.myntra.com{href}"
            if not href.startswith("https://") or href in seen_urls:
                continue

            # Title
            title = ""
            # Try brand + name combination (Myntra structure)
            brand_el = item.find(class_=lambda x: x and "product-brand" in x)
            name_el = item.find(class_=lambda x: x and "product-product" in x)
            if brand_el and name_el:
                title = f"{brand_el.text.strip()} {name_el.text.strip()}"
            if not title:
                h3 = item.find("h3")
                if h3:
                    title = h3.text.strip()
            if not title:
                img = item.find("img", alt=True)
                if img:
                    title = img.get("alt", "").strip()
            if not title or len(title) < 4:
                continue

            # Price
            price: Optional[float] = None
            price_el = item.find(class_=lambda x: x and "product-discountedPrice" in x)
            if not price_el:
                price_el = item.find(class_=lambda x: x and "product-price" in x)
            if price_el:
                price = _parse_numeric_price(price_el.text)
            if price is None:
                m = re.search(r'[\u20B9]\s*([0-9,]+)', item.text)
                if m:
                    price = _parse_numeric_price(m.group(1))

            # Rating
            rating: Optional[float] = None
            rating_el = item.find(class_=lambda x: x and "product-ratingsCount" in x)
            if not rating_el:
                rating_el = item.find(
                    lambda t: t.name in ["div", "span"]
                    and re.match(r'^[1-5](\.[0-9])?$', t.text.strip())
                )
            if rating_el:
                rating = _parse_numeric_rating(rating_el.text)

            seen_urls.add(href)
            results.append({
                "platform": "Myntra",
                "product_title": title,
                "product_url": href,
                "price": price or 0.0,
                "rating": rating,
                "availability": True,
                "product_details": [],
                "ranking": len(results) + 1,
            })

            logger.info(
                f"[MYNTRA_SCRAPER] Candidate rank={len(results)}: '{title[:40]}' "
                f"price={price} rating={rating}"
            )

        except Exception as item_err:
            logger.debug(f"[MYNTRA_SCRAPER] Item parse error: {item_err}")
            continue

    return results


def _parse_myntra_detail_page(html: str, url: str) -> Dict[str, Any]:
    """
    Parse Myntra product detail page.
    Priority: JSON-LD -> semantic HTML -> CSS fallbacks.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")
    product_name = ""
    price: Optional[float] = None
    rating: Optional[float] = None
    details_list: List[str] = []
    availability = True

    # LAYER 1: JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "Product":
                    if not product_name and item.get("name"):
                        product_name = item["name"].strip()
                    desc = item.get("description", "")
                    if desc and len(desc.strip()) > 10:
                        details_list.append(desc.strip())
                    offers = item.get("offers", {})
                    if isinstance(offers, dict):
                        if price is None and offers.get("price"):
                            price = _parse_numeric_price(offers["price"])
                        if "OutOfStock" in str(offers.get("availability", "")):
                            availability = False
                    agg = item.get("aggregateRating", {})
                    if isinstance(agg, dict) and rating is None:
                        rating = _parse_numeric_rating(agg.get("ratingValue"))
        except Exception:
            pass

    # LAYER 2: DOM fallbacks
    if not product_name:
        h1 = soup.find("h1")
        if h1:
            product_name = h1.text.strip()
        if not product_name and soup.title:
            title_text = soup.title.string.split("|")[0].strip()
            if len(title_text) > 3:
                product_name = title_text

    if price is None:
        price_el = soup.find(class_=lambda x: x and "pdp-price" in x)
        if not price_el:
            price_el = soup.find(class_=lambda x: x and "product-price" in x)
        if price_el:
            price = _parse_numeric_price(price_el.text)
        if price is None:
            m = re.search(r'[\u20B9]\s*([0-9,]+)', soup.text)
            if m:
                price = _parse_numeric_price(m.group(1))

    if rating is None:
        rating_el = soup.find(class_=lambda x: x and "index-overallRating" in x)
        if not rating_el:
            rating_el = soup.find(
                lambda t: t.name in ["div", "span"]
                and re.match(r'^[1-5](\.[0-9])?$', t.text.strip())
            )
        if rating_el:
            rating = _parse_numeric_rating(rating_el.text)

    # Product details -- Myntra uses a size/description section
    desc_el = soup.find(class_=lambda x: x and "pdp-product-description-content" in x)
    if desc_el:
        txt = desc_el.text.strip()
        if txt and len(txt) > 15:
            details_list.append(txt)

    # Specifications list
    spec_lists = soup.find_all("li", class_=lambda x: x and "pdp-sizeguide" in x)
    for li in spec_lists:
        txt = li.text.strip()
        if txt and txt not in details_list:
            details_list.append(txt)

    # Generic ul/li extraction -- always accumulate, not gated by JSON-LD results
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li"):
            txt = li.text.strip()
            if txt and len(txt) > 5 and txt not in details_list:
                details_list.append(txt)

    # Availability
    page_text = soup.text.lower()
    if any(phrase in page_text for phrase in ["sold out", "currently out of stock", "out of stock"]):
        availability = False

    return {
        "product_name": product_name,
        "price": price,
        "rating": rating,
        "product_details": details_list,
        "availability": availability,
    }


class MyntraScraper(BaseScraper):
    """
    Myntra-specific scraper extending BaseScraper.
    Handles Myntra search navigation, product card extraction,
    detail page enrichment, and login-wall/block detection.
    """

    PLATFORM = "Myntra"

    def __init__(self, headless: bool = True, timeout_ms: Optional[int] = None, max_retries: int = 2):
        super().__init__(
            headless=headless,
            timeout_ms=timeout_ms or 30000,
            max_retries=max_retries,
        )

    async def _fetch_search_candidates(
        self, page: Any, query: str
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Navigate to Myntra search, scroll for lazy loading, parse candidate cards.
        Returns (candidates, is_blocked).
        """
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.myntra.com/{encoded_query}"

        logger.info(f"[MYNTRA_SCRAPING_STARTED] Navigating to search URL: {search_url}")
        self.search_result_status = "PARSE_ERROR"
        nav_ok = await self.safe_navigate(page, search_url, wait_until="domcontentloaded")

        if not nav_ok:
            self.search_result_status = self.last_navigation_status or "NETWORK_ERROR"
            logger.error("[MYNTRA_SCRAPING_FAILED] reason=page_navigation_error")
            return [], False

        await page.wait_for_timeout(2500)
        await self.scroll_page(page, count=4, delay_ms=800)

        current_url = page.url
        html = await page.content()

        if not html or len(html) < 500:
            self.search_result_status = self.last_navigation_status or "PARSE_ERROR"
            logger.error("[MYNTRA_SCRAPING_FAILED] reason=empty_page_content")
            return [], False

        if _is_myntra_blocked(html, current_url):
            logger.warning(
                f"[MYNTRA_SCRAPING_BLOCKED] Block/login-wall detected at URL: {current_url[:80]}"
            )
            return [], True

        logger.info("[MYNTRA_PAGE_RENDERED] Search page content captured successfully.")
        candidates = _parse_myntra_search_page(html)
        if not candidates:
            self.search_result_status = self.last_navigation_status or self.classify_empty_search(html)
        logger.info(f"[MYNTRA_DATA_EXTRACTED] Search page candidates discovered: {len(candidates)}")
        return candidates, False

    async def _fetch_detail_page(
        self, page: Any, cand: Dict[str, Any], idx: int, total: int
    ) -> Dict[str, Any]:
        """
        Fetch and parse a single Myntra product detail page.
        Returns merged product dict (detail enriched + search card fallback).
        """
        cand_url = cand.get("product_url", "")
        if not cand_url:
            logger.warning(f"[MYNTRA_SCRAPER] Skipping candidate {idx}/{total}: empty URL.")
            return {}

        logger.info(f"[MYNTRA_SCRAPER] Detail page {idx}/{total}: {cand_url[:80]}")

        await page.wait_for_timeout(1500)

        try:
            await page.goto(cand_url, wait_until="commit", timeout=20000)
            await page.wait_for_timeout(1500)
            detail_html = await page.content()
        except Exception as e:
            logger.warning(
                f"[MYNTRA_SCRAPER] Detail page load failed for {cand_url[:60]}: {e}. "
                "Using search card fallback."
            )
            detail_html = ""

        if detail_html and _is_myntra_blocked(detail_html, page.url):
            self.challenge_detected = True
            logger.warning(f"[MYNTRA_SCRAPING_BLOCKED] Detail page blocked for {cand_url[:60]}")
            detail_html = ""

        detailed: Dict[str, Any] = {}
        if detail_html and len(detail_html) > 500:
            logger.info(f"[MYNTRA_PAGE_RENDERED] Detail page rendered for candidate {idx}/{total}.")
            detailed = _parse_myntra_detail_page(detail_html, cand_url)

        scraped_at = datetime.now(timezone.utc).isoformat()

        merged = {
            "platform": "Myntra",
            "product_title": detailed.get("product_name") or cand.get("product_title", ""),
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
                f"[MYNTRA_DATA_EXTRACTED] Product {idx}: '{merged['product_title'][:40]}' "
                f"price={merged['price']} rating={merged['rating']}"
            )

        return merged

    async def scrape_myntra(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Main scrape entry point for Myntra.
        Returns a platform result dict:
          Success: {"platform": "Myntra", "status": "OK", "products": [...]}
          Blocked: {"platform": "Myntra", "status": "BLOCKED", "products": [], "error": "..."}
          Failed:  {"platform": "Myntra", "status": "FAILED",  "products": [], "error": "..."}
        """
        limit = min(max_products or DEFAULT_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
        query = build_myntra_query(product_name, category, product_details)

        logger.info(
            f"[MYNTRA_SCRAPING_STARTED] product='{product_name}' "
            f"query='{query}' max_products={limit}"
        )

        products: List[Dict[str, Any]] = []
        self.challenge_detected = False

        try:
            async with async_playwright() as p:
                browser, context, page = await self.create_browser_session(p)

                try:
                    candidates, is_blocked = await self._fetch_search_candidates(page, query)

                    if is_blocked:
                        return {
                            "platform": "Myntra",
                            "status": "BLOCKED",
                            "products": [],
                            "error": "Login wall or automation challenge detected",
                        }

                    if not candidates:
                        logger.warning(
                            f"[MYNTRA_SCRAPING_FAILED] reason=empty_search_results query='{query}'"
                        )
                        return {
                            "platform": "Myntra",
                            "status": self.search_result_status,
                            "products": [],
                            "error": self.search_failure_message(),
                        }

                    selected = candidates[:limit]
                    logger.info(
                        f"[MYNTRA_SCRAPER] Processing {len(selected)} candidates "
                        f"(of {len(candidates)} found)"
                    )

                    for idx, cand in enumerate(selected, 1):
                        product = await self._fetch_detail_page(page, cand, idx, len(selected))
                        if product and product.get("product_title"):
                            products.append(product)

                    if self.challenge_detected:
                        return {"platform": "Myntra", "status": "BLOCKED", "products": [], "error": "Security challenge detected on a product detail page"}

                finally:
                    await self.close_session(browser, context)

        except Exception as e:
            logger.error(
                f"[MYNTRA_SCRAPING_FAILED] Unexpected error during scraping: {e}",
                exc_info=True,
            )
            return {
                "platform": "Myntra",
                "status": "FAILED",
                "products": [],
                "error": str(e),
            }

        logger.info(
            f"[MYNTRA_SCRAPING_COMPLETED] product='{product_name}' "
            f"results_collected={len(products)}"
        )
        return {"platform": "Myntra", "status": "OK", "products": products}

    async def scrape_myntra_with_retry(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Retry wrapper for scrape_myntra.
        - Retries on transient failures.
        - Does NOT retry BLOCKED responses.
        - Uses exponential backoff between attempts.
        """
        last_result: Dict[str, Any] = {
            "platform": "Myntra",
            "status": "FAILED",
            "products": [],
            "error": "No attempts made",
        }

        for attempt_num in range(1, self.max_retries + 1):
            logger.info(f"[MYNTRA_SCRAPER] Scrape attempt {attempt_num}/{self.max_retries}")
            try:
                result = await self.scrape_myntra(
                    product_name, category, product_details, max_products
                )
                status = self.classify_status(result.get("status"), result.get("error"))
                result = {**result, "status": status}
                last_result = result

                if status == "BLOCKED":
                    logger.warning(
                        f"[MYNTRA_SCRAPING_BLOCKED] Platform blocked on attempt {attempt_num}. "
                        "Not retrying immediately."
                    )
                    return result

                if status == "SUCCESS" and result.get("products"):
                    return result

                if status not in {"NETWORK_ERROR", "TIMEOUT"}:
                    return result

                logger.warning(
                    f"[MYNTRA_SCRAPER] Attempt {attempt_num} returned empty results. "
                    f"{'Retrying...' if attempt_num < self.max_retries else 'All attempts exhausted.'}"
                )
            except Exception as e:
                status = self.classify_status("FAILED", e)
                last_result = {
                    "platform": "Myntra",
                    "status": status,
                    "products": [],
                    "error": str(e),
                }
                if status not in {"NETWORK_ERROR", "TIMEOUT"}:
                    return last_result
                logger.warning(f"[MYNTRA_SCRAPER] Attempt {attempt_num} raised exception: {e}")

            if attempt_num < self.max_retries:
                wait_secs = 2 ** attempt_num
                logger.info(f"[MYNTRA_SCRAPER] Retrying in {wait_secs}s...")
                await asyncio.sleep(wait_secs)

        logger.error(
            f"[MYNTRA_SCRAPING_FAILED] All {self.max_retries} attempts failed for '{product_name}'."
        )
        return last_result
