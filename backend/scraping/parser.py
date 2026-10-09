"""
parser.py
=========
Resilient, dynamic BeautifulSoup parsing for Flipkart search and detail pages.

Architecture:
1. Dynamic container discovery:
   - Primary: Anchor discovery via '/p/' or 'itm' product URLs + ancestor price-bearing container.
   - Secondary: Attribute-based discovery ('data-id').
   - Tertiary: Structural DOM relationship fallback.
2. Dynamic field extraction with layered fallbacks:
   - Product Name: anchor title -> img alt -> heading tags -> anchor text.
   - Price: regex on currency symbols (₹, Rs.) -> attribute matching.
   - Rating: numeric badge regex (^[1-5](\.[0-9])?$) -> aria-label -> validated [0, 5].
   - Product Details: JSON-LD Product schema -> specs table -> highlights -> description.
   - Ranking: sequential position in search results preserved.
   - Availability: in-stock / out-of-stock detection.
3. No hardcoded product names, brittle single-class assumptions, or fabricated values.
4. Structured logging: [SCRAPER].
"""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


def clean_flipkart_url(raw_url: Optional[str]) -> str:
    """
    Normalize product URLs by stripping ephemeral search/tracking query parameters,
    preserving canonical path (/product-slug/p/itm...) and pid.
    """
    if not raw_url:
        return ""
    raw_url = raw_url.strip()
    if not raw_url:
        return ""
    if raw_url.startswith("/"):
        raw_url = f"https://www.flipkart.com{raw_url}"

    try:
        parsed = urllib.parse.urlparse(raw_url)
        qs = urllib.parse.parse_qs(parsed.query)
        pid = qs.get("pid", [None])[0]
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if pid:
            clean_url += f"?pid={pid}"
        return clean_url
    except Exception:
        return raw_url


def parse_numeric_rating(val: Any) -> Optional[float]:
    """
    Parse and validate numeric rating strictly between 0.0 and 5.0.
    Returns None if missing, out of bounds, or unparseable.
    """
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str:
        return None

    match = re.search(r'([0-5](?:\.\d+)?)', val_str)
    if match:
        try:
            rating = float(match.group(1))
            if 0.0 <= rating <= 5.0:
                return round(rating, 2)
        except (ValueError, TypeError):
            pass
    return None


def parse_numeric_price(val: Any) -> Optional[float]:
    """
    Parse positive numeric price from raw text or numbers.
    Returns None if missing or non-positive.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 0 else None

    val_str = str(val).replace(",", "").strip()
    # Match ₹120 or Rs. 120 or plain 120
    match = re.search(r'(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d{1,2})?)', val_str, re.IGNORECASE)
    if match:
        try:
            p = float(match.group(1))
            return p if p > 0 else None
        except (ValueError, TypeError):
            pass
    return None


def discover_product_containers(soup: BeautifulSoup) -> List[Tag]:
    """
    Dynamically discover product card containers using multi-signal heuristics:
    1. Primary: Containers with product anchor (/p/ or itm) AND price indicator.
    2. Fallback: Elements with data-id attributes.
    3. Structural deduplication: Avoid nested cards.
    """
    containers: List[Tag] = []
    seen_urls = set()

    # Heuristic 1: Find all product anchors
    product_anchors = soup.find_all("a", href=lambda h: h and ("/p/" in h or "itm" in h))
    for a in product_anchors:
        raw_href = a.get("href", "")
        clean_url = clean_flipkart_url(raw_href)
        base_url = clean_url.split("?")[0]
        if not base_url or base_url in seen_urls:
            continue

        # Walk up the DOM tree (up to 6 levels) to find the card container enclosing a price
        current = a
        card_container: Optional[Tag] = None
        for _ in range(6):
            parent = current.parent
            if not parent or not isinstance(parent, Tag) or parent.name in ["body", "html"]:
                break

            # If parent contains other product links, stop before entering the multi-card container
            other_anchors = [
                anc for anc in parent.find_all("a", href=lambda h: h and ("/p/" in h or "itm" in h))
                if clean_flipkart_url(anc.get("href", "")).split("?")[0] != base_url
            ]
            if other_anchors:
                break

            # Check if this parent encloses currency text
            if "₹" in parent.text or re.search(r'rs\.?\s*\d+', parent.text, re.IGNORECASE):
                card_container = parent
                # If parent has data-id, it's definitely the card
                if parent.has_attr("data-id"):
                    break
            current = parent

        target_container = card_container or a
        if target_container not in containers:
            seen_urls.add(base_url)
            containers.append(target_container)


    # Heuristic 2: Check data-id elements if Heuristic 1 found very few (< 3)
    if len(containers) < 3:
        data_id_cards = soup.find_all("div", attrs={"data-id": True})
        for card in data_id_cards:
            if card not in containers:
                a_tag = card.find("a", href=lambda h: h and ("/p/" in h or "itm" in h))
                if a_tag:
                    raw_href = a_tag.get("href", "")
                    clean_url = clean_flipkart_url(raw_href)
                    base_url = clean_url.split("?")[0]
                    if base_url and base_url not in seen_urls:
                        seen_urls.add(base_url)
                        containers.append(card)

    return containers


def extract_title_from_container(container: Tag) -> str:
    """
    Extract product title dynamically using layered fallbacks:
    1. Product anchor title attribute
    2. img alt attribute
    3. Prominent heading (h1, h2, h3)
    4. Text inside product anchor
    5. Fallback known class names
    """
    # 1. Anchor with title
    a_with_title = container.find("a", attrs={"title": True})
    if a_with_title and a_with_title.get("title", "").strip():
        return a_with_title.get("title").strip()

    # 2. Image with alt text
    img = container.find("img", alt=True)
    if img and img.get("alt", "").strip() and len(img.get("alt").strip()) > 3:
        return img.get("alt").strip()

    # 3. Headings
    for h_tag in ["h2", "h3", "h1"]:
        heading = container.find(h_tag)
        if heading and heading.text.strip() and len(heading.text.strip()) > 3:
            return heading.text.strip()

    # 4. Product link text
    prod_a = container.find("a", href=lambda h: h and ("/p/" in h or "itm" in h))
    if prod_a and prod_a.text.strip() and len(prod_a.text.strip()) > 4:
        return prod_a.text.strip()

    # 5. Known compiled classes fallback
    title_el = container.find(class_=lambda x: x and any(c in x for c in [
        "atJtCj", "IRpwTa", "s1Q9rs", "WKTcLC", "CGtC98", "_4rR01T", "KzDlHZ", "wjcEIp"
    ]))
    if title_el and title_el.text.strip():
        return title_el.text.strip()

    return ""


def extract_price_from_container(container: Tag) -> Optional[float]:
    """
    Extract price dynamically using regex pattern matching on currency symbols.
    """
    # Look for elements containing ₹
    price_tags = container.find_all(lambda t: t.name in ["div", "span", "p"] and "₹" in t.text and len(t.text.strip()) < 30)
    for tag in price_tags:
        p = parse_numeric_price(tag.text)
        if p is not None:
            return p

    # Fallback to full container text regex
    match = re.search(r'₹\s*([0-9,]+(?:\.[0-9]+)?)', container.text)
    if match:
        return parse_numeric_price(match.group(1))

    return None


def extract_rating_from_container(container: Tag) -> Optional[float]:
    """
    Extract rating dynamically:
    1. Elements with text matching '^[1-5](\.[0-9])?$'
    2. aria-label containing rating pattern
    3. Fallback known rating classes
    """
    # 1. Exact numeric rating badge pattern (e.g. "4.2", "4.5", "5")
    rating_node = container.find(
        lambda t: t.name in ["div", "span"] and re.match(r'^[1-5](\.[0-9])?$', t.text.strip())
    )
    if rating_node:
        r = parse_numeric_rating(rating_node.text)
        if r is not None:
            return r

    # 2. aria-label (e.g. "4.2 stars", "4.2 out of 5")
    aria_node = container.find(attrs={"aria-label": re.compile(r'[0-5](?:\.\d+)?\s*(?:star|out of)', re.I)})
    if aria_node:
        r = parse_numeric_rating(aria_node.get("aria-label"))
        if r is not None:
            return r

    # 3. Known rating class fallback
    rating_el = container.find(["div", "span"], class_=lambda x: x and any(c in x for c in ["XQDdHH", "_3LWZlK", "v1zwn21o"]))
    if rating_el:
        return parse_numeric_rating(rating_el.text)

    return None


def parse_search_page(html: str, query: str = "") -> List[Dict[str, Any]]:
    """
    Parse rendered Flipkart search page HTML dynamically.
    Returns list of candidate product dictionaries with ranking preserved.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    containers = discover_product_containers(soup)
    logger.info(f"[SCRAPER] Product candidates discovered: {len(containers)} containers found on search page")

    results: List[Dict[str, Any]] = []
    seen_urls = set()

    for idx, card in enumerate(containers, start=1):
        try:
            # 1. Product Link
            a_tag = card.find("a", href=lambda h: h and ("/p/" in h or "itm" in h))
            if not a_tag:
                a_tag = card.find("a", href=True)
            if not a_tag or not a_tag.get("href"):
                logger.debug(f"[SCRAPER] Field unavailable: product_url missing on card {idx}")
                continue

            raw_href = a_tag.get("href")
            clean_url = clean_flipkart_url(raw_href)
            base_url = clean_url.split("?")[0]
            if not base_url or base_url in seen_urls:
                continue

            # 2. Product Name
            title = extract_title_from_container(card)
            if not title:
                logger.debug(f"[SCRAPER] Field unavailable: product_name missing on card {idx} — card skipped")
                continue

            # 3. Price
            price = extract_price_from_container(card)
            if price is None:
                logger.debug(f"[SCRAPER] Field unavailable: price missing on card {idx} ('{title[:30]}')")

            # 4. Rating
            rating = extract_rating_from_container(card)
            if rating is None:
                logger.debug(f"[SCRAPER] Field unavailable: rating missing on card {idx} ('{title[:30]}')")

            seen_urls.add(base_url)
            item = {
                "platform_name": "Flipkart",
                "product_name": title,
                "product_url": clean_url,
                "product_details": [],
                "price": price or 0.0,
                "rating": rating,
                "availability": None,
                "ranking": len(results) + 1,  # Sequential ranking in search results
            }
            results.append(item)
            logger.info(
                f"[SCRAPER] Product extracted: rank={item['ranking']} name='{title[:35]}' "
                f"price={price} rating={rating}"
            )

        except Exception as card_err:
            logger.warning(f"[SCRAPER] Malformed product card skipped at index {idx}: {card_err}")
            continue

    logger.info(f"[SCRAPER] Scraping completed for search page: {len(results)} valid products extracted")
    return results


def parse_detail_page(html: str, url: str) -> Dict[str, Any]:
    """
    Parse a rendered Flipkart product detail page HTML dynamically.
    Extracts product_name, price, rating, details, and availability.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")
    clean_url = clean_flipkart_url(url)
    logger.info(f"[SCRAPER] Detail page extraction starting for URL: {clean_url[:80]}")

    product_name = ""
    price: Optional[float] = None
    rating: Optional[float] = None
    product_details_list: List[str] = []
    availability = None

    # ──────────────────────────────────────────────────────────────────────────
    # LAYER 1: Parse structured schema metadata (<script type="application/ld+json">)
    # ──────────────────────────────────────────────────────────────────────────
    ld_scripts = soup.find_all("script", type="application/ld+json")
    for script_tag in ld_scripts:
        try:
            if not script_tag.string:
                continue
            data = json.loads(script_tag.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "Product":
                    if not product_name and item.get("name"):
                        product_name = item.get("name").strip()

                    desc = item.get("description")
                    if desc and isinstance(desc, str) and len(desc.strip()) > 10:
                        product_details_list.append(desc.strip())

                    offers = item.get("offers")
                    if isinstance(offers, dict):
                        if price is None and offers.get("price"):
                            price = parse_numeric_price(offers.get("price"))
                        avail_str = str(offers.get("availability", ""))
                        if "OutOfStock" in avail_str:
                            availability = False
                        elif "InStock" in avail_str:
                            availability = True

                    agg_rating = item.get("aggregateRating")
                    if isinstance(agg_rating, dict) and rating is None:
                        rating = parse_numeric_rating(agg_rating.get("ratingValue"))
        except Exception as e:
            logger.debug(f"[SCRAPER] JSON-LD schema parsing notice: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # LAYER 2: DOM fallback & enrichment
    # ──────────────────────────────────────────────────────────────────────────
    # Product Name
    if not product_name:
        h1 = soup.find("h1")
        if h1 and h1.text.strip():
            product_name = h1.text.strip()
            logger.info("[SCRAPER] Fallback extraction used: h1 for product_name")
        elif soup.title and soup.title.string:
            title_text = soup.title.string.split("-")[0].strip()
            if len(title_text) > 3:
                product_name = title_text
                logger.info("[SCRAPER] Fallback extraction used: <title> tag for product_name")

    # Price
    if price is None:
        # Search for price element with currency symbol
        price_tag = soup.find(lambda t: t.name in ["div", "span"] and "₹" in t.text and len(t.text.strip()) < 25)
        if price_tag:
            price = parse_numeric_price(price_tag.text)
            logger.info(f"[SCRAPER] Fallback extraction used: DOM currency text for price ({price})")
        else:
            # Fallback class check
            price_el = soup.find(class_=lambda x: x and any(c in x for c in ["Nx9bqj", "_30jeq3", "CxhGGd", "_16Jk6d"]))
            if price_el:
                price = parse_numeric_price(price_el.text)

    # Rating (strictly 0 - 5)
    if rating is None:
        # 1. Check badge with exact rating regex
        badge = soup.find(lambda t: t.name in ["div", "span"] and re.match(r'^[1-5]\.[0-9]$', t.text.strip()))
        if badge:
            rating = parse_numeric_rating(badge.text)
            logger.info(f"[SCRAPER] Fallback extraction used: regex badge for rating ({rating})")

        # 2. Check aria-label containing rating (e.g. "4.8 stars", "4.8 out of 5")
        if rating is None:
            aria_el = soup.find(attrs={"aria-label": re.compile(r'[0-5](?:\.\d+)?\s*(?:star|out of|\/)', re.I)})
            if aria_el:
                rating = parse_numeric_rating(aria_el.get("aria-label"))
                logger.info(f"[SCRAPER] Fallback extraction used: aria-label for rating ({rating})")

        # 3. Check text containing "Rated 4.8" or "4.8 / 5"
        if rating is None:
            rated_node = soup.find(
                lambda t: t.name in ["div", "span", "p"] and re.search(r'(?:rated|rating:?)\s*([1-5](?:\.[0-9])?)', t.text, re.I)
            )
            if rated_node:
                m = re.search(r'(?:rated|rating:?)\s*([1-5](?:\.[0-9])?)', rated_node.text, re.I)
                if m:
                    rating = parse_numeric_rating(m.group(1))
                    logger.info(f"[SCRAPER] Fallback extraction used: text pattern for rating ({rating})")

        # 4. Fallback known rating classes
        if rating is None:
            rating_el = soup.find(["div", "span"], class_=lambda x: x and any(c in x for c in ["XQDdHH", "_3LWZlK", "v1zwn21o"]))
            if rating_el:
                rating = parse_numeric_rating(rating_el.text)


    # Product Details: Specifications, Highlights, Description
    # 1. Specifications table
    spec_tables = soup.find_all("table")
    for table in spec_tables:
        for tr in table.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                k = tds[0].text.strip()
                v = tds[1].text.strip()
                if k and v and len(v) < 150:
                    spec_str = f"{k}: {v}"
                    if spec_str not in product_details_list:
                        product_details_list.append(spec_str)

    # Fallback to row-like div structures for specs
    if not product_details_list:
        spec_rows = soup.find_all("tr", class_=lambda x: x and any(c in x for c in ["_1sftAk", "row", "WJdYP6"]))
        for tr in spec_rows:
            tds = tr.find_all("td")
            if len(tds) >= 2:
                k = tds[0].text.strip()
                v = tds[1].text.strip()
                if k and v and len(v) < 100:
                    spec_str = f"{k}: {v}"
                    if spec_str not in product_details_list:
                        product_details_list.append(spec_str)

    # 2. Highlights list
    highlights_div = soup.find(["div", "ul"], class_=lambda x: x and any(c in x for c in ["_2418kt", "X3BRps", "_21Ahn-"]))
    if highlights_div:
        for li in highlights_div.find_all("li"):
            txt = li.text.strip()
            if txt and txt not in product_details_list:
                product_details_list.append(txt)

    # Generic ul/ol extraction -- always accumulate alongside JSON-LD and spec table data
    for ul in soup.find_all(["ul", "ol"]):
        for li in ul.find_all("li"):
            txt = li.text.strip()
            if txt and len(txt) > 3 and txt not in product_details_list:
                product_details_list.append(txt)


    # 3. Product Description section
    desc_el = soup.find(["div", "p"], class_=lambda x: x and any(c in x for c in ["_1mXcCf", "RmoJUa", "_3la3Fn"]))
    if desc_el:
        desc_text = desc_el.text.strip()
        if desc_text and len(desc_text) > 15 and desc_text not in product_details_list:
            product_details_list.append(desc_text)

    # Availability check from DOM text
    page_text = soup.text.lower()
    if any(phrase in page_text for phrase in ["currently out of stock", "sold out", "item is out of stock"]):
        availability = False

    if rating is None:
        logger.info(f"[SCRAPER] Field unavailable: rating on detail page for '{product_name[:30]}'")
    if not product_details_list:
        logger.info(f"[SCRAPER] Field unavailable: product_details on detail page for '{product_name[:30]}'")

    logger.info(
        f"[SCRAPER] Detail page extraction completed for '{product_name[:35]}': "
        f"price={price} rating={rating} details_count={len(product_details_list)}"
    )

    return {
        "platform_name": "Flipkart",
        "product_name": product_name,
        "product_url": clean_url,
        "product_details": product_details_list,
        "price": price,
        "rating": rating,
        "availability": availability,
    }
