"""
meesho_scraper.py
=================
Meesho-specific dynamic scraper built on top of BaseScraper.

Responsibilities:
- Inherits Playwright browser lifecycle from BaseScraper
- Generates dynamic search queries from ProductCatalog fields
- Navigates Meesho search and product detail pages
- Resilient multi-layer HTML parsing (JSON-LD -> semantic -> CSS fallbacks)
- CAPTCHA / bot-block / empty-page detection with structured failure result
- Retries temporary failures; does NOT retry CAPTCHA pages
- Emits all required structured log tags

Required log tags:
  MEESHO_SCRAPING_STARTED
  MEESHO_PAGE_RENDERED
  MEESHO_DATA_EXTRACTED
  MEESHO_SCRAPING_BLOCKED
  MEESHO_SCRAPING_FAILED
  MEESHO_SCRAPING_COMPLETED
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

MEESHO_BLOCK_SIGNALS = [
    "verify you are human",
    "access denied",
    "unusual traffic",
    "robot check",
    "captcha",
    "403 forbidden",
    "challenge",
    "automated access",
    "login to continue",
    "please sign in",
]


def build_meesho_query(
    product_name: str,
    category: Optional[str] = None,
    product_details: Optional[str] = None,
) -> str:
    """
    Dynamically compose a Meesho search query from ProductCatalog fields.

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
    logger.info(f"[MEESHO_SCRAPER] Dynamic search query built: '{query}'")
    return query


def _is_meesho_blocked(html: str, page_url: str = "") -> bool:
    """
    Detect Meesho CAPTCHA and bot-block states.
    Returns True if the page appears to be a block/challenge page.
    """
    if not html:
        return False
    lower_html = html.lower()
    for signal in MEESHO_BLOCK_SIGNALS:
        if signal in lower_html:
            return True
    if "/login" in page_url and "meesho.com" in page_url:
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


def _parse_meesho_search_page(html: str) -> List[Dict[str, Any]]:
    """
    Parse rendered Meesho search results HTML.
    Multi-layer approach:
      1. JSON-LD Product schema
      2. Semantic product card containers with data attributes
      3. Anchor-based discovery with price heuristics
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, Any]] = []
    seen_urls: set = set()

    # Layer 1: JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string or len(results) >= HARD_MAX_PRODUCTS:
                break
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") in ("Product", "ItemList"):
                    if item.get("@type") == "ItemList":
                        # Iterate itemListElement
                        for elem in item.get("itemListElement", []):
                            if isinstance(elem, dict) and elem.get("@type") == "Product":
                                items.append(elem)
                        continue
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
                        "platform": "Meesho",
                        "product_title": name,
                        "product_url": url if url.startswith("http") else f"https://meesho.com{url}",
                        "price": price or 0.0,
                        "rating": rating,
                        "availability": True,
                        "product_details": [],
                        "ranking": len(results) + 1,
                    })
        except Exception:
            pass

    if results:
        logger.info(f"[MEESHO_SCRAPER] JSON-LD yielded {len(results)} candidates")
        return results

    # Layer 2: Semantic HTML - Meesho uses a React SPA so look for
    # data-testid attributes and product card containers
    card_selectors = [
        lambda tag: tag.name == "div" and tag.get("data-testid") == "product-container",
        lambda tag: tag.name == "div" and tag.get("data-testid") == "product-card",
    ]

    product_cards: List[Any] = []
    for selector in card_selectors:
        cards = soup.find_all(selector)
        if cards:
            product_cards = cards
            break

    # Broader fallback: anchor-based discovery
    if not product_cards:
        product_anchors = soup.find_all(
            "a",
            href=lambda h: h and ("/p/" in (h or "") or "product" in (h or "").lower())
        )
        # Walk up to find card containers
        seen_containers: set = set()
        for a in product_anchors:
            parent = a.parent
            if parent and id(parent) not in seen_containers:
                seen_containers.add(id(parent))
                product_cards.append(parent)

    logger.info(f"[MEESHO_SCRAPER] Found {len(product_cards)} semantic product containers")

    for card in product_cards:
        try:
            a_tag = card.find("a", href=True)
            if not a_tag:
                a_tag = card if card.name == "a" else None
            if not a_tag or not a_tag.get("href"):
                continue

            href = a_tag.get("href", "")
            if href.startswith("/"):
                href = f"https://meesho.com{href}"
            if not href.startswith("http") or href in seen_urls:
                continue

            # Title
            title = ""
            h4 = card.find("h4")
            h3 = card.find("h3")
            if h4:
                title = h4.text.strip()
            elif h3:
                title = h3.text.strip()
            if not title:
                # Try p tags -- Meesho often uses <p> for product names
                for p in card.find_all("p"):
                    txt = p.text.strip()
                    if txt and len(txt) > 4:
                        title = txt
                        break
            if not title:
                img = card.find("img", alt=True)
                if img:
                    title = img.get("alt", "").strip()
            if not title or len(title) < 4:
                continue

            # Price -- look for currency symbol
            price: Optional[float] = None
            m = re.search(r'[\u20B9]\s*([0-9,]+)', card.text)
            if m:
                price = _parse_numeric_price(m.group(1))
            if price is None:
                price_el = card.find(
                    lambda t: t.name in ["p", "span", "div"]
                    and re.search(r'\d{2,}', t.text)
                    and len(t.text.strip()) < 20
                )
                if price_el:
                    price = _parse_numeric_price(price_el.text)

            # Rating
            rating: Optional[float] = None
            rating_el = card.find(
                lambda t: t.name in ["p", "span"]
                and re.match(r'^[1-5](\.[0-9])?$', t.text.strip())
            )
            if rating_el:
                rating = _parse_numeric_rating(rating_el.text)

            seen_urls.add(href)
            results.append({
                "platform": "Meesho",
                "product_title": title,
                "product_url": href,
                "price": price or 0.0,
                "rating": rating,
                "availability": True,
                "product_details": [],
                "ranking": len(results) + 1,
            })

            logger.info(
                f"[MEESHO_SCRAPER] Candidate rank={len(results)}: '{title[:40]}' "
                f"price={price} rating={rating}"
            )

        except Exception as card_err:
            logger.debug(f"[MEESHO_SCRAPER] Card parse error: {card_err}")
            continue

    return results


def _parse_meesho_detail_page(html: str, url: str) -> Dict[str, Any]:
    """
    Parse Meesho product detail page.
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
        m = re.search(r'[\u20B9]\s*([0-9,]+)', soup.text)
        if m:
            price = _parse_numeric_price(m.group(1))

    if rating is None:
        rating_el = soup.find(
            lambda t: t.name in ["p", "span", "div"]
            and re.match(r'^[1-5](\.[0-9])?$', t.text.strip())
        )
        if rating_el:
            rating = _parse_numeric_rating(rating_el.text)

    # Product description/highlights
    desc_section = soup.find(attrs={"data-testid": "product-description"})
    if desc_section:
        txt = desc_section.text.strip()
        if txt and len(txt) > 10:
            details_list.append(txt)

    # Generic ul/li extraction -- always accumulate, not gated by JSON-LD results
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li"):
            txt = li.text.strip()
            if txt and len(txt) > 5 and txt not in details_list:
                details_list.append(txt)

    page_text = soup.text.lower()
    if any(phrase in page_text for phrase in ["sold out", "out of stock", "currently unavailable"]):
        availability = False

    return {
        "product_name": product_name,
        "price": price,
        "rating": rating,
        "product_details": details_list,
        "availability": availability,
    }


class MeeshoScraper(BaseScraper):
    """
    Meesho-specific scraper extending BaseScraper.
    Handles Meesho search navigation, product card extraction,
    detail page enrichment, and bot-block detection.
    """

    PLATFORM = "Meesho"

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
        Navigate to Meesho search, scroll for lazy loading, parse candidate cards.
        Returns (candidates, is_blocked).
        """
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://meesho.com/search?q={encoded_query}"

        logger.info(f"[MEESHO_SCRAPING_STARTED] Navigating to search URL: {search_url}")
        nav_ok = await self.safe_navigate(page, search_url, wait_until="domcontentloaded")

        if not nav_ok:
            logger.error("[MEESHO_SCRAPING_FAILED] reason=page_navigation_error")
            return [], False

        # Meesho is a React SPA -- wait longer for hydration
        await page.wait_for_timeout(3000)
        await self.scroll_page(page, count=4, delay_ms=900)

        current_url = page.url
        html = await page.content()

        if not html or len(html) < 500:
            logger.error("[MEESHO_SCRAPING_FAILED] reason=empty_page_content")
            return [], False

        if _is_meesho_blocked(html, current_url):
            logger.warning(
                f"[MEESHO_SCRAPING_BLOCKED] Block/challenge detected at URL: {current_url[:80]}"
            )
            return [], True

        logger.info("[MEESHO_PAGE_RENDERED] Search page content captured successfully.")
        candidates = _parse_meesho_search_page(html)
        logger.info(f"[MEESHO_DATA_EXTRACTED] Search page candidates discovered: {len(candidates)}")
        return candidates, False

    async def _fetch_detail_page(
        self, page: Any, cand: Dict[str, Any], idx: int, total: int
    ) -> Dict[str, Any]:
        """
        Fetch and parse a single Meesho product detail page.
        Returns merged product dict (detail enriched + search card fallback).
        """
        cand_url = cand.get("product_url", "")
        if not cand_url:
            logger.warning(f"[MEESHO_SCRAPER] Skipping candidate {idx}/{total}: empty URL.")
            return {}

        logger.info(f"[MEESHO_SCRAPER] Detail page {idx}/{total}: {cand_url[:80]}")

        await page.wait_for_timeout(1500)

        try:
            await page.goto(cand_url, wait_until="commit", timeout=20000)
            await page.wait_for_timeout(2000)
            detail_html = await page.content()
        except Exception as e:
            logger.warning(
                f"[MEESHO_SCRAPER] Detail page load failed for {cand_url[:60]}: {e}. "
                "Using search card fallback."
            )
            detail_html = ""

        if detail_html and _is_meesho_blocked(detail_html, page.url):
            logger.warning(f"[MEESHO_SCRAPING_BLOCKED] Detail page blocked for {cand_url[:60]}")
            detail_html = ""

        detailed: Dict[str, Any] = {}
        if detail_html and len(detail_html) > 500:
            logger.info(f"[MEESHO_PAGE_RENDERED] Detail page rendered for candidate {idx}/{total}.")
            detailed = _parse_meesho_detail_page(detail_html, cand_url)

        scraped_at = datetime.now(timezone.utc).isoformat()

        merged = {
            "platform": "Meesho",
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
                f"[MEESHO_DATA_EXTRACTED] Product {idx}: '{merged['product_title'][:40]}' "
                f"price={merged['price']} rating={merged['rating']}"
            )

        return merged

    async def scrape_meesho(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Main scrape entry point for Meesho.
        Returns a platform result dict:
          Success: {"platform": "Meesho", "status": "OK", "products": [...]}
          Blocked: {"platform": "Meesho", "status": "BLOCKED", "products": [], "error": "..."}
          Failed:  {"platform": "Meesho", "status": "FAILED",  "products": [], "error": "..."}
        """
        limit = min(max_products or DEFAULT_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
        query = build_meesho_query(product_name, category, product_details)

        logger.info(
            f"[MEESHO_SCRAPING_STARTED] product='{product_name}' "
            f"query='{query}' max_products={limit}"
        )

        products: List[Dict[str, Any]] = []

        try:
            async with async_playwright() as p:
                browser, context, page = await self.create_browser_session(p)

                try:
                    candidates, is_blocked = await self._fetch_search_candidates(page, query)

                    if is_blocked:
                        return {
                            "platform": "Meesho",
                            "status": "BLOCKED",
                            "products": [],
                            "error": "Automation challenge detected on search page",
                        }

                    if not candidates:
                        logger.warning(
                            f"[MEESHO_SCRAPING_FAILED] reason=empty_search_results query='{query}'"
                        )
                        return {
                            "platform": "Meesho",
                            "status": "FAILED",
                            "products": [],
                            "error": "No search results found",
                        }

                    selected = candidates[:limit]
                    logger.info(
                        f"[MEESHO_SCRAPER] Processing {len(selected)} candidates "
                        f"(of {len(candidates)} found)"
                    )

                    for idx, cand in enumerate(selected, 1):
                        product = await self._fetch_detail_page(page, cand, idx, len(selected))
                        if product and product.get("product_title"):
                            products.append(product)

                finally:
                    await self.close_session(browser, context)

        except Exception as e:
            logger.error(
                f"[MEESHO_SCRAPING_FAILED] Unexpected error during scraping: {e}",
                exc_info=True,
            )
            return {
                "platform": "Meesho",
                "status": "FAILED",
                "products": [],
                "error": str(e),
            }

        logger.info(
            f"[MEESHO_SCRAPING_COMPLETED] product='{product_name}' "
            f"results_collected={len(products)}"
        )
        return {"platform": "Meesho", "status": "OK", "products": products}

    async def scrape_meesho_with_retry(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Retry wrapper for scrape_meesho.
        - Retries on transient failures.
        - Does NOT retry BLOCKED responses.
        - Uses exponential backoff between attempts.
        """
        last_result: Dict[str, Any] = {
            "platform": "Meesho",
            "status": "FAILED",
            "products": [],
            "error": "No attempts made",
        }

        for attempt_num in range(1, self.max_retries + 1):
            logger.info(f"[MEESHO_SCRAPER] Scrape attempt {attempt_num}/{self.max_retries}")
            try:
                result = await self.scrape_meesho(
                    product_name, category, product_details, max_products
                )
                last_result = result

                if result.get("status") == "BLOCKED":
                    logger.warning(
                        f"[MEESHO_SCRAPING_BLOCKED] Platform blocked on attempt {attempt_num}. "
                        "Not retrying immediately."
                    )
                    return result

                if result.get("status") == "OK" and result.get("products"):
                    return result

                logger.warning(
                    f"[MEESHO_SCRAPER] Attempt {attempt_num} returned empty results. "
                    f"{'Retrying...' if attempt_num < self.max_retries else 'All attempts exhausted.'}"
                )
            except Exception as e:
                last_result = {
                    "platform": "Meesho",
                    "status": "FAILED",
                    "products": [],
                    "error": str(e),
                }
                logger.warning(f"[MEESHO_SCRAPER] Attempt {attempt_num} raised exception: {e}")

            if attempt_num < self.max_retries:
                wait_secs = 2 ** attempt_num
                logger.info(f"[MEESHO_SCRAPER] Retrying in {wait_secs}s...")
                await asyncio.sleep(wait_secs)

        logger.error(
            f"[MEESHO_SCRAPING_FAILED] All {self.max_retries} attempts failed for '{product_name}'."
        )
        return last_result
