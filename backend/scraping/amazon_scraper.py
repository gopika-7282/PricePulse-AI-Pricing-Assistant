


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
from scraping.query_builder import focused_product_query
from scraping.validator import validate_scraped_product
from app.services.relevance_filter_service import filter_candidate_products

logger = logging.getLogger(__name__)

# Maximum products per platform - enforced hard cap at 10
DEFAULT_MAX_PRODUCTS = 10
HARD_MAX_PRODUCTS = 10


def select_amazon_candidates(
    candidates: List[Dict[str, Any]],
    product_name: str,
    category: Optional[str],
    max_products: Optional[int],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Deduplicate, validate and relevance-filter the full search result before capping."""
    limit = min(max_products or DEFAULT_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
    unique: List[Dict[str, Any]] = []
    seen_ids = set()
    for candidate in candidates:
        asin = str(candidate.get("asin") or "").strip().upper()
        url = str(candidate.get("product_url") or "").strip().rstrip("/").casefold()
        identity = ("asin", asin) if asin else (("url", url) if url else None)
        if identity is None or identity in seen_ids:
            continue
        seen_ids.add(identity)
        unique.append(candidate)

    validated: List[Dict[str, Any]] = []
    for candidate in unique:
        normalized = validate_scraped_product(candidate)
        if normalized:
            validated.append({**candidate, **normalized})

    relevant = filter_candidate_products(
        target_name=product_name,
        target_category=category or "",
        candidates=validated,
    )
    # Amazon's search result position is the existing relevance ranking. Preserve it
    # while applying the hard cap only after validation and relevance filtering.
    ranked = sorted(relevant, key=lambda product: product.get("ranking", 0))
    selected = ranked[:limit]
    counts = {
        "parsed": len(candidates),
        "unique": len(unique),
        "validated": len(validated),
        "relevant": len(relevant),
        "ranked": len(ranked),
        "selected": len(selected),
    }
    logger.info(
        "[AMAZON_CANDIDATE_COUNTS] "
        + " ".join(f"{name}={count}" for name, count in counts.items())
    )
    return selected, counts

# Signals that Amazon is blocking automation
AMAZON_BLOCK_SIGNALS = [
    "enter the characters you see below",
    "sorry, we just need to make sure you",
    "type the characters you see in this image",
    "robot check",
    "to discuss automated access to amazon",
    "access denied",
    "verify your identity",
    "unusual traffic",
    "please enable javascript",
    "automated access",
    "captcha",
]


def build_amazon_query(
    product_name: str,
    category: Optional[str] = None,
    product_details: Optional[str] = None,
) -> str:
    """
    Focused query builder for Amazon: 2-3 focused terms from product name and type.
    """
    focused = focused_product_query(product_name, category, product_details)
    logger.info("[AMAZON_SCRAPER] Focused search query built")
    return focused


def _is_amazon_blocked(html: str, page_url: str = "") -> bool:
    """
    Detect common Amazon automation-block / CAPTCHA states.
    Returns True if the page appears to be a block/challenge page.
    Checks URL patterns first (reliable), then HTML body signals.
    """
    # URL-based block detection (reliable even if html is empty)
    if page_url and ("errors/validateCaptcha" in page_url or "/ap/cvf/" in page_url):
        return True
    if not html:
        return False
    lower_html = html.lower()
    for signal in AMAZON_BLOCK_SIGNALS:
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


def _parse_amazon_search_page(html: str) -> List[Dict[str, Any]]:
    """
    Parse rendered Amazon search results HTML.
    Multi-layer approach:
      1. data-asin attribute containers (stable Amazon semantic attribute)
      2. CSS fallbacks for price/rating
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: List[Dict[str, Any]] = []
    seen_asins: set = set()

    # Primary: Amazon product cards identified by data-asin
    product_cards = soup.find_all(
        "div",
        attrs={"data-asin": lambda v: v and len(v) > 3},
    )
    logger.info(f"[AMAZON_SCRAPER] Found {len(product_cards)} data-asin containers on search page")

    for card in product_cards:
        try:
            asin = card.get("data-asin", "").strip()
            if not asin or asin in seen_asins:
                continue

            # Product link -- prefer canonical /dp/ URL; avoid /sspa/click (sponsored redirects)
            # Always build canonical URL from ASIN if available (avoids bot-challenge redirects)
            if asin:
                clean_url = f"https://www.amazon.in/dp/{asin}"
            else:
                a_tag = card.find("a", href=lambda h: h and "/dp/" in h)
                if not a_tag:
                    a_tag = card.find("a", href=True)
                if not a_tag or not a_tag.get("href"):
                    continue
                href = a_tag.get("href", "")
                if href.startswith("/"):
                    href = f"https://www.amazon.in{href}"
                parsed = urllib.parse.urlparse(href)
                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if not clean_url.startswith("https://"):
                    continue

            # Title -- try h2 first (Amazon search uses h2 for titles)
            title = ""
            h2 = card.find("h2")
            if h2:
                span = h2.find("span")
                title = (span.text if span else h2.text).strip()
            if not title:
                img = card.find("img", alt=True)
                if img:
                    title = img.get("alt", "").strip()
            if not title or len(title) < 4:
                continue

            # Price -- Amazon uses .a-price-whole / .a-offscreen
            price: Optional[float] = None
            price_whole = card.find(class_=lambda x: x and "a-price-whole" in x)
            if price_whole:
                price = _parse_numeric_price(price_whole.text)
            if price is None:
                offscreen = card.find(class_=lambda x: x and "a-offscreen" in x)
                if offscreen:
                    price = _parse_numeric_price(offscreen.text)
            if price is None:
                # Currency symbol fallback in card text
                m = re.search(r'[\u20B9]\s*([0-9,]+)', card.text)
                if m:
                    price = _parse_numeric_price(m.group(1))

            # Rating
            rating: Optional[float] = None
            rating_el = card.find(
                attrs={"aria-label": re.compile(r'[0-5](?:\.\d+)?\s*out of\s*5', re.I)}
            )
            if rating_el:
                rating = _parse_numeric_rating(rating_el.get("aria-label"))
            if rating is None:
                rating_span = card.find("span", class_=lambda x: x and "a-icon-alt" in x)
                if rating_span:
                    rating = _parse_numeric_rating(rating_span.text)

            seen_asins.add(asin)
            results.append({
                "platform": "Amazon",
                "product_title": title,
                "product_url": clean_url,
                "price": price or 0.0,
                "rating": rating,
                "availability": None,
                "product_details": [],
                "asin": asin,
                "ranking": len(results) + 1,
            })

            logger.debug("[AMAZON_SCRAPER] Candidate parsed rank=%d", len(results))

        except Exception as card_err:
            logger.debug("[AMAZON_SCRAPER] Card parse error: exception_class=%s", type(card_err).__name__)
            continue

    return results


def _parse_amazon_detail_page(html: str, url: str) -> Dict[str, Any]:
    """
    Parse Amazon product detail page.
    Priority: JSON-LD -> semantic HTML -> CSS fallbacks.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")
    product_name = ""
    price: Optional[float] = None
    rating: Optional[float] = None
    details_list: List[str] = []
    availability = None

    # LAYER 1: JSON-LD structured data
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
                        offer_state = str(offers.get("availability", "")).casefold()
                        if "outofstock" in offer_state:
                            availability = False
                        elif "instock" in offer_state:
                            availability = True
                    agg = item.get("aggregateRating", {})
                    if isinstance(agg, dict) and rating is None:
                        rating = _parse_numeric_rating(agg.get("ratingValue"))
        except Exception:
            pass

    # LAYER 2: DOM fallbacks
    if not product_name:
        el = soup.find(id="productTitle") or soup.find("h1")
        if el:
            product_name = el.text.strip()

    if price is None:
        for price_id in ["priceblock_ourprice", "priceblock_dealprice", "price_inside_buybox"]:
            el = soup.find(id=price_id)
            if el:
                price = _parse_numeric_price(el.text)
                break
        if price is None:
            offscreen = soup.find(class_=lambda x: x and "a-offscreen" in x)
            if offscreen:
                price = _parse_numeric_price(offscreen.text)
        if price is None:
            m = re.search(r'[\u20B9]\s*([0-9,]+)', soup.text)
            if m:
                price = _parse_numeric_price(m.group(1))

    if rating is None:
        rating_el = soup.find(id="acrPopover") or soup.find(
            attrs={"data-hook": "rating-out-of-text"}
        )
        if rating_el:
            rating = _parse_numeric_rating(rating_el.get("title") or rating_el.text)
        if rating is None:
            aria_el = soup.find(
                attrs={"aria-label": re.compile(r'[0-5](?:\.\d+)?\s*out of\s*5', re.I)}
            )
            if aria_el:
                rating = _parse_numeric_rating(aria_el.get("aria-label"))

    # Feature bullets (product details)
    bullets = soup.find(id="feature-bullets") or soup.find(id="featurebullets_feature_div")
    if bullets:
        for li in bullets.find_all("li"):
            txt = li.text.strip()
            if txt and len(txt) > 3 and txt not in details_list:
                details_list.append(txt)

    # Technical details / specs table
    for table in soup.find_all("table", id=lambda x: x and "productDetails" in x):
        for tr in table.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                spec = f"{th.text.strip()}: {td.text.strip()}"
                if len(spec) < 200 and spec not in details_list:
                    details_list.append(spec)

    # Generic ul/li extraction -- always accumulate alongside Amazon-specific sources
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li"):
            txt = li.text.strip()
            if txt and len(txt) > 5 and txt not in details_list:
                details_list.append(txt)

    # Availability
    avail_el = soup.find(id="availability")
    if avail_el:
        availability_text = avail_el.text.casefold()
        if any(phrase in availability_text for phrase in ["currently unavailable", "out of stock"]):
            availability = False
        elif "in stock" in availability_text:
            availability = True

    return {
        "product_name": product_name,
        "price": price,
        "rating": rating,
        "product_details": details_list,
        "availability": availability,
    }


class AmazonScraper(BaseScraper):
    """
    Amazon India-specific scraper extending BaseScraper.
    Handles Amazon.in search navigation, product card extraction,
    detail page enrichment, and bot-block detection.
    """

    PLATFORM = "Amazon"

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
        Navigate to Amazon.in search, scroll for lazy loading, parse candidate cards.
        Returns (candidates, is_blocked).
        """
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.amazon.in/s?k={encoded_query}"

        logger.info("[AMAZON_SCRAPING_STARTED] platform=Amazon stage=search")
        self.search_result_status = "PARSE_ERROR"
        nav_ok = await self.safe_navigate(page, search_url, wait_until="domcontentloaded", platform="Amazon", stage="search")

        if not nav_ok:
            self.search_result_status = self.last_navigation_status or "NETWORK_ERROR"
            self.log_diagnostic("Amazon", "search", requested_url=search_url,
                                final_url=getattr(page, "url", None), navigation="failure",
                                challenge=None, challenge_reason="not_checked_navigation_failure",
                                product_card_count=0, normalized_product_count=0,
                                reason_category=self.result_reason_category(self.search_result_status))
            logger.error("[AMAZON_SCRAPING_FAILED] reason=page_navigation_error")
            return [], False

        # Small delay to allow dynamic content to settle
        await page.wait_for_timeout(2000)
        await self.scroll_page(page, count=3, delay_ms=900)

        current_url = page.url
        html = await page.content()

        if not html or len(html) < 500:
            self.search_result_status = self.last_navigation_status or "PARSE_ERROR"
            self.log_diagnostic("Amazon", "search", requested_url=search_url, final_url=current_url,
                                challenge=None, challenge_reason="not_checked_short_or_empty_page",
                                product_card_count=0, normalized_product_count=0,
                                reason_category="empty_page_content")
            logger.error("[AMAZON_SCRAPING_FAILED] reason=empty_page_content")
            return [], False

        # Block detection -- check before parsing
        challenge_detected = _is_amazon_blocked(html, current_url)
        self.log_diagnostic("Amazon", "search", requested_url=search_url, final_url=current_url,
                            challenge=challenge_detected,
                            challenge_reason=self.challenge_reason_category(
                                challenge_detected, html, current_url,
                                url_markers=("errors/validateCaptcha", "/ap/cvf/"),
                                html_markers=AMAZON_BLOCK_SIGNALS,
                            ))
        if challenge_detected:
            return [], True

        logger.info("[AMAZON_PAGE_RENDERED] Search page content captured successfully.")
        candidates = _parse_amazon_search_page(html)
        if not candidates:
            self.search_result_status = self.last_navigation_status or self.classify_empty_search(html)
        self.log_diagnostic("Amazon", "search_parse", requested_url=search_url, final_url=current_url,
                            product_card_count=len(candidates))
        return candidates, False

    async def _fetch_detail_page(
        self, page: Any, cand: Dict[str, Any], idx: int, total: int
    ) -> Dict[str, Any]:
        """
        Fetch and parse a single Amazon product detail page.
        Returns merged product dict (detail enriched + search card fallback).
        """
        cand_url = cand.get("product_url", "")
        if not cand_url:
            logger.warning(f"[AMAZON_SCRAPER] Skipping candidate {idx}/{total}: empty URL.")
            return {}

        logger.info("[AMAZON_SCRAPER] platform=Amazon stage=detail candidate=%d/%d", idx, total)

        # Keep a bounded pause between product detail requests.
        await page.wait_for_timeout(1000)

        try:
            await page.goto(cand_url, wait_until="domcontentloaded", timeout=10000)
        except Exception as e:
            self.log_diagnostic("Amazon", "detail", requested_url=cand_url,
                                final_url=getattr(page, "url", None), navigation="failure",
                                exception=e, reason_category="detail_navigation_failure")
            logger.warning("[AMAZON_SCRAPER] Detail page load failed; using search card fallback. exception_class=%s",
                           type(e).__name__)
            detail_html = ""
        else:
            self.log_diagnostic("Amazon", "detail", requested_url=cand_url,
                                final_url=getattr(page, "url", None), navigation="success")
            try:
                await page.wait_for_timeout(500)
                detail_html = await page.content()
            except Exception as e:
                self.log_diagnostic("Amazon", "detail_capture", requested_url=cand_url,
                                    final_url=getattr(page, "url", None), navigation="success",
                                    exception=e, reason_category="page_capture_failure")
                detail_html = ""

        # Check if detail page is also blocked
        detail_challenge = _is_amazon_blocked(detail_html, page.url) if detail_html else False
        self.log_diagnostic("Amazon", "detail", requested_url=cand_url,
                            final_url=getattr(page, "url", None), challenge=detail_challenge,
                            challenge_reason=self.challenge_reason_category(
                                detail_challenge, detail_html, getattr(page, "url", ""),
                                url_markers=("errors/validateCaptcha", "/ap/cvf/"),
                                html_markers=AMAZON_BLOCK_SIGNALS,
                            ) if detail_html else "not_checked_no_html")
        if detail_challenge:
            self.challenge_detected = True
            detail_html = ""

        detailed: Dict[str, Any] = {}
        if detail_html and len(detail_html) > 500:
            logger.info(f"[AMAZON_PAGE_RENDERED] Detail page rendered for candidate {idx}/{total}.")
            detailed = _parse_amazon_detail_page(detail_html, cand_url)

        scraped_at = datetime.now(timezone.utc).isoformat()

        merged = {
            "platform": "Amazon",
            "product_title": detailed.get("product_name") or cand.get("product_title", ""),
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

        self.log_diagnostic("Amazon", "detail_parse", requested_url=cand_url,
                            final_url=getattr(page, "url", None),
                            normalized_product_count=1 if merged["product_title"] else 0,
                            reason_category="parsed" if merged["product_title"] else "missing_product_title")

        return merged

    async def _enrich_selected_candidates(
        self, page: Any, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Enrich each selected result independently; retain valid search-card data on failure."""
        products: List[Dict[str, Any]] = []
        for idx, candidate in enumerate(candidates, 1):
            try:
                product = await self._fetch_detail_page(page, candidate, idx, len(candidates))
            except Exception as exc:
                logger.warning("[AMAZON_SCRAPER] Detail enrichment failed for candidate %s/%s; using search card data. exception_class=%s",
                               idx, len(candidates), type(exc).__name__)
                product = {}
            if not product or not product.get("product_title"):
                product = {
                    **candidate,
                    "platform": "Amazon",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                }
            if product.get("product_title"):
                products.append(product)
        return products

    async def scrape_amazon(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Main scrape entry point for Amazon.
        Returns a platform result dict:
          Success: {"platform": "Amazon", "status": "OK", "products": [...]}
          Blocked: {"platform": "Amazon", "status": "BLOCKED", "products": [], "error": "..."}
          Failed:  {"platform": "Amazon", "status": "FAILED",  "products": [], "error": "..."}
        """
        limit = min(max_products or DEFAULT_MAX_PRODUCTS, HARD_MAX_PRODUCTS)
        query = build_amazon_query(product_name, category, product_details)

        logger.info("[AMAZON_SCRAPING_STARTED] platform=Amazon stage=scrape max_products=%d", limit)

        products: List[Dict[str, Any]] = []
        self.challenge_detected = False

        try:
            async with async_playwright() as p:
                browser, context, page = await self.create_browser_session(p)

                try:
                    # Phase 1: Search page
                    candidates, is_blocked = await self._fetch_search_candidates(page, query)

                    if is_blocked:
                        return {
                            "platform": "Amazon",
                            "status": "BLOCKED",
                            "products": [],
                            "error": "Automation challenge detected on search page",
                        }

                    if not candidates:
                        logger.warning(
                            f"[AMAZON_SCRAPING_FAILED] reason=empty_search_results query='{query}'"
                        )
                        return {
                            "platform": "Amazon",
                            "status": self.search_result_status,
                            "products": [],
                            "error": self.search_failure_message(),
                        }

                    selected, _candidate_counts = select_amazon_candidates(
                        candidates, product_name, category, limit
                    )
                    self.log_diagnostic("Amazon", "candidate_selection", product_card_count=len(candidates),
                                        normalized_product_count=len(selected), reason_category="relevance_filtered")

                    # Phase 2: Detail pages
                    products = await self._enrich_selected_candidates(page, selected)

                    if self.challenge_detected:
                        return {"platform": "Amazon", "status": "BLOCKED", "products": [], "error": "Security challenge detected on a product detail page"}

                finally:
                    await self.close_session(browser, context)

        except asyncio.TimeoutError as te:
            logger.error("[AMAZON_SCRAPING_FAILED] platform=Amazon reason_category=timeout exception_class=%s",
                         type(te).__name__)
            return {
                "platform": "Amazon",
                "status": "TIMEOUT",
                "products": [],
                "error": str(te),
            }
        except Exception as e:
            logger.error("[AMAZON_SCRAPING_FAILED] platform=Amazon reason_category=scraper_failure exception_class=%s",
                         type(e).__name__)
            return {
                "platform": "Amazon",
                "status": "PARSE_ERROR",
                "products": [],
                "error": str(e),
            }

        self.log_diagnostic("Amazon", "scrape", normalized_product_count=len(products),
                            final_status="SUCCESS" if products else "EMPTY",
                            reason_category="success" if products else "no_products")
        return {
            "platform": "Amazon",
            "status": "SUCCESS" if products else "EMPTY",
            "products": products,
            "error": None if products else "No valid products extracted",
        }

    async def scrape_amazon_with_retry(
        self,
        product_name: str,
        category: Optional[str] = None,
        product_details: Optional[str] = None,
        max_products: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Retry wrapper for scrape_amazon.
        - Retries on transient failures.
        - Does NOT retry CAPTCHA / BLOCKED responses.
        - Uses exponential backoff between attempts.
        """
        last_result: Dict[str, Any] = {
            "platform": "Amazon",
            "status": "EMPTY",
            "products": [],
            "error": "No attempts made",
        }

        for attempt_num in range(1, self.max_retries + 1):
            logger.info("[AMAZON_SCRAPER] platform=Amazon stage=attempt attempt=%d/%d", attempt_num, self.max_retries)
            try:
                result = await self.scrape_amazon(
                    product_name, category, product_details, max_products
                )
                status = self.classify_status(result.get("status"), result.get("error"))
                result = {**result, "status": status}
                last_result = result
                self.log_diagnostic("Amazon", "attempt_result", final_status=status,
                                    reason_category=self.result_reason_category(status),
                                    normalized_product_count=len(result.get("products") or []))

                # Do NOT retry if blocked
                if result.get("status") == "BLOCKED":
                    logger.warning(
                        f"[AMAZON_SCRAPING_BLOCKED] Platform blocked on attempt {attempt_num}. "
                        "Not retrying immediately."
                    )
                    return result

                if status == "SUCCESS":
                    return result

                if status not in {"NETWORK_ERROR", "TIMEOUT"}:
                    return result

                logger.warning(
                    f"[AMAZON_SCRAPER] Attempt {attempt_num} returned empty results. "
                    f"{'Retrying...' if attempt_num < self.max_retries else 'All attempts exhausted.'}"
                )
            except Exception as e:
                status = self.classify_status("FAILED", e)
                last_result = {
                    "platform": "Amazon",
                    "status": status,
                    "products": [],
                    "error": str(e),
                }
                if status not in {"NETWORK_ERROR", "TIMEOUT"}:
                    return last_result
                self.log_diagnostic("Amazon", "attempt_result", final_status=status,
                                    reason_category=self.result_reason_category(status), exception=e)

            if attempt_num < self.max_retries:
                wait_secs = 2 ** attempt_num
                logger.info(f"[AMAZON_SCRAPER] Retrying in {wait_secs}s...")
                await asyncio.sleep(wait_secs)

        logger.error("[AMAZON_SCRAPING_FAILED] platform=Amazon reason_category=%s status=%s",
                     self.result_reason_category(last_result.get("status")), last_result.get("status"))
        return last_result
