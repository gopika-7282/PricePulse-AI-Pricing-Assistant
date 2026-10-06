"""
test_product_details_parsing.py
================================
Focused regression tests for the product_details accumulation fix.

Root cause:
  parser.py / _parse_myntra_detail_page / _parse_meesho_detail_page all had:
      if not details_list:
          <ul/li DOM extraction>
  This meant when JSON-LD supplied even a short description, richer DOM spec
  details were silently skipped.

  scraping_service.py serialised json.dumps([]) = "[]" for empty lists.

Fix applied:
  - Removed the `if not details_list:` guard from generic ul/li DOM extraction
    in parser.py (Flipkart), myntra_scraper.py, meesho_scraper.py.
  - amazon_scraper.py was already correct (feature-bullets run unconditionally).
  - scraping_service.py: empty list now produces "" not "[]".

Tests:
  1. JSON-LD has description + DOM has richer details -> both accumulated
  2. JSON-LD has no description + DOM has details -> DOM details returned
  3. JSON-LD + DOM have overlapping data -> no duplicates created
  4. Neither source has details -> empty list (not None/error)
  5. ScrapingService: empty list -> "" not "[]"
  6. Validator passes product_details through unchanged
"""

import json
import pytest
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Parser imports
# ---------------------------------------------------------------------------
from scraping.parser import parse_detail_page
from scraping.myntra_scraper import _parse_myntra_detail_page
from scraping.meesho_scraper import _parse_meesho_detail_page
from scraping.amazon_scraper import _parse_amazon_detail_page
from scraping.validator import validate_scraped_product, validate_products


# ===========================================================================
# Shared HTML helpers
# ===========================================================================

def _flipkart_html(jsonld_desc: Optional[str], ul_items: List[str]) -> str:
    ld_desc = f'"description": "{jsonld_desc}",' if jsonld_desc else ""
    lis = "".join(f"<li>{item}</li>" for item in ul_items)
    return f"""
    <html><body>
    <script type="application/ld+json">
    {{"@type": "Product", "name": "Test Shirt", {ld_desc}
     "offers": {{"price": "499", "availability": "InStock"}},
     "aggregateRating": {{"ratingValue": "4.2"}}}}
    </script>
    <ul>{lis}</ul>
    </body></html>
    """


def _myntra_html(jsonld_desc: Optional[str], ul_items: List[str]) -> str:
    ld_desc = f'"description": "{jsonld_desc}",' if jsonld_desc else ""
    lis = "".join(f"<li>{item}</li>" for item in ul_items)
    return f"""
    <html><body>
    <script type="application/ld+json">
    {{"@type": "Product", "name": "Myntra Dress", {ld_desc}
     "offers": {{"price": "999", "availability": "InStock"}},
     "aggregateRating": {{"ratingValue": "4.1"}}}}
    </script>
    <ul>{lis}</ul>
    </body></html>
    """


def _meesho_html(jsonld_desc: Optional[str], ul_items: List[str]) -> str:
    ld_desc = f'"description": "{jsonld_desc}",' if jsonld_desc else ""
    lis = "".join(f"<li>{item}</li>" for item in ul_items)
    return f"""
    <html><body>
    <script type="application/ld+json">
    {{"@type": "Product", "name": "Meesho Kurti", {ld_desc}
     "offers": {{"price": "399", "availability": "InStock"}}}}
    </script>
    <ul>{lis}</ul>
    </body></html>
    """


def _amazon_html(jsonld_desc: Optional[str], bullet_items: List[str]) -> str:
    ld_desc = f'"description": "{jsonld_desc}",' if jsonld_desc else ""
    lis = "".join(f"<li>{item}</li>" for item in bullet_items)
    return f"""
    <html><body>
    <script type="application/ld+json">
    {{"@type": "Product", "name": "Amazon Book", {ld_desc}
     "offers": {{"price": "299", "availability": "InStock"}},
     "aggregateRating": {{"ratingValue": "4.7"}}}}
    </script>
    <div id="feature-bullets"><ul>{lis}</ul></div>
    </body></html>
    """


FLIPKART_URL = "https://www.flipkart.com/test/p/abc123"
MYNTRA_URL   = "https://www.myntra.com/product/123"
MEESHO_URL   = "https://meesho.com/kurti/p/456"
AMAZON_URL   = "https://www.amazon.in/dp/B001234567"

DOM_SPECS = ["100% Cotton", "Machine Washable", "Regular Fit"]
LD_DESC   = "Premium quality cotton shirt for everyday wear."


# ===========================================================================
# 1. Flipkart (parse_detail_page in parser.py)
# ===========================================================================

class TestFlipkartParserDetails:

    def test_jsonld_no_desc_dom_ul_captured(self):
        """JSON-LD without description -> DOM <ul><li> details returned."""
        html = _flipkart_html(jsonld_desc=None, ul_items=DOM_SPECS)
        result = parse_detail_page(html, FLIPKART_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list), "product_details must be a list"
        assert len(details) > 0, f"Expected DOM specs, got empty list"
        assert any("Cotton" in d for d in details), f"Missing DOM spec: {details}"

    def test_jsonld_desc_and_dom_ul_both_accumulated(self):
        """JSON-LD description present -> DOM <ul><li> items ALSO accumulated."""
        html = _flipkart_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = parse_detail_page(html, FLIPKART_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list)
        # Must have both the JSON-LD description AND the DOM items
        has_ld  = any("cotton shirt" in d.lower() for d in details)
        has_dom = any("Cotton" in d for d in details)
        assert has_ld,  f"Missing JSON-LD description in: {details}"
        assert has_dom, f"Missing DOM spec item in: {details}"

    def test_no_duplicates_when_both_present(self):
        """No duplicate entries even when JSON-LD and DOM both provide data."""
        html = _flipkart_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = parse_detail_page(html, FLIPKART_URL)
        details = result.get("product_details", [])
        assert len(details) == len(set(details)), f"Duplicates found: {details}"

    def test_neither_source_returns_empty_list(self):
        """No JSON-LD desc, no DOM specs -> empty list (not None, not error)."""
        html = "<html><body><h1>Test Shirt</h1><span>Rs499</span></body></html>"
        result = parse_detail_page(html, FLIPKART_URL)
        details = result.get("product_details", None)
        assert isinstance(details, list), "product_details should always be a list"

    def test_other_fields_unaffected(self):
        """product_name, price, rating still extracted correctly."""
        html = _flipkart_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = parse_detail_page(html, FLIPKART_URL)
        assert result.get("product_name") == "Test Shirt"
        assert result.get("price") == 499.0
        assert result.get("rating") == 4.2


# ===========================================================================
# 2. Myntra (_parse_myntra_detail_page in myntra_scraper.py)
# ===========================================================================

class TestMyntraParserDetails:

    def test_jsonld_no_desc_dom_ul_captured(self):
        """Myntra: No JSON-LD description -> DOM <ul><li> details returned."""
        html = _myntra_html(jsonld_desc=None, ul_items=DOM_SPECS)
        result = _parse_myntra_detail_page(html, MYNTRA_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list)
        assert len(details) > 0, f"Expected DOM specs, got: {details}"
        assert any("Cotton" in d for d in details), f"Missing spec: {details}"

    def test_jsonld_desc_and_dom_ul_both_accumulated(self):
        """Myntra: JSON-LD description present -> DOM items STILL accumulated."""
        html = _myntra_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = _parse_myntra_detail_page(html, MYNTRA_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list)
        has_ld  = any("cotton shirt" in d.lower() for d in details)
        has_dom = any("Cotton" in d for d in details)
        assert has_ld,  f"Missing JSON-LD description in: {details}"
        assert has_dom, f"Missing DOM spec item in: {details}"

    def test_no_duplicates(self):
        """Myntra: No duplicate entries."""
        html = _myntra_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = _parse_myntra_detail_page(html, MYNTRA_URL)
        details = result.get("product_details", [])
        assert len(details) == len(set(details)), f"Duplicates: {details}"

    def test_neither_source_returns_empty_list(self):
        """Myntra: No sources -> empty list."""
        html = "<html><body><h1>Myntra Dress</h1></body></html>"
        result = _parse_myntra_detail_page(html, MYNTRA_URL)
        assert isinstance(result.get("product_details", []), list)


# ===========================================================================
# 3. Meesho (_parse_meesho_detail_page in meesho_scraper.py)
# ===========================================================================

class TestMeeshoParserDetails:

    def test_jsonld_no_desc_dom_ul_captured(self):
        """Meesho: No JSON-LD description -> DOM <ul><li> details returned."""
        html = _meesho_html(jsonld_desc=None, ul_items=DOM_SPECS)
        result = _parse_meesho_detail_page(html, MEESHO_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list)
        assert len(details) > 0, f"Expected DOM specs, got: {details}"
        assert any("Cotton" in d for d in details), f"Missing spec: {details}"

    def test_jsonld_desc_and_dom_ul_both_accumulated(self):
        """Meesho: JSON-LD description present -> DOM items STILL accumulated (was the bug)."""
        html = _meesho_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = _parse_meesho_detail_page(html, MEESHO_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list)
        has_ld  = any("cotton shirt" in d.lower() for d in details)
        has_dom = any("Cotton" in d for d in details)
        assert has_ld,  f"Missing JSON-LD description in: {details}"
        assert has_dom, f"Missing DOM spec item in: {details}"

    def test_no_duplicates(self):
        """Meesho: No duplicate entries."""
        html = _meesho_html(jsonld_desc=LD_DESC, ul_items=DOM_SPECS)
        result = _parse_meesho_detail_page(html, MEESHO_URL)
        details = result.get("product_details", [])
        assert len(details) == len(set(details)), f"Duplicates: {details}"

    def test_neither_source_returns_empty_list(self):
        """Meesho: No sources -> empty list."""
        html = "<html><body><h1>Meesho Kurti</h1></body></html>"
        result = _parse_meesho_detail_page(html, MEESHO_URL)
        assert isinstance(result.get("product_details", []), list)


# ===========================================================================
# 4. Amazon (_parse_amazon_detail_page in amazon_scraper.py)
# ===========================================================================

class TestAmazonParserDetails:

    def test_jsonld_desc_and_bullets_both_accumulated(self):
        """Amazon: JSON-LD description AND feature bullets both accumulated."""
        bullets = ["Hardcover edition", "352 pages", "Publisher: Penguin"]
        html = _amazon_html(jsonld_desc="Bestselling novel.", bullet_items=bullets)
        result = _parse_amazon_detail_page(html, AMAZON_URL)
        details = result.get("product_details", [])
        assert isinstance(details, list)
        has_ld      = any("bestselling" in d.lower() for d in details)
        has_bullets = any("Hardcover" in d for d in details)
        assert has_ld,      f"Missing JSON-LD desc: {details}"
        assert has_bullets, f"Missing bullet item: {details}"

    def test_no_duplicates(self):
        """Amazon: No duplicates."""
        bullets = ["Hardcover edition", "352 pages"]
        html = _amazon_html(jsonld_desc="A great novel.", bullet_items=bullets)
        result = _parse_amazon_detail_page(html, AMAZON_URL)
        details = result.get("product_details", [])
        assert len(details) == len(set(details))


# ===========================================================================
# 5. Validator preserves product_details unchanged
# ===========================================================================

def _make_valid_raw(details) -> Dict[str, Any]:
    return {
        "platform": "Flipkart",
        "product_title": "Test Product",
        "product_url": "https://www.flipkart.com/test/p/abc123",
        "price": 499.0,
        "rating": 4.2,
        "availability": True,
        "product_details": details,
        "scraped_at": "2025-01-01T00:00:00+00:00",
    }


class TestValidatorPreservesDetails:

    def test_non_empty_list_passes_through(self):
        """Validator does not strip non-empty product_details."""
        raw = _make_valid_raw(["100% Cotton", "Machine Washable"])
        result = validate_scraped_product(raw)
        assert result is not None
        assert result["product_details"] == ["100% Cotton", "Machine Washable"]

    def test_empty_list_passes_through(self):
        """Validator preserves empty list (does not convert to None)."""
        raw = _make_valid_raw([])
        result = validate_scraped_product(raw)
        assert result is not None
        assert result["product_details"] == []

    def test_missing_details_defaults_to_empty_list(self):
        """product_details absent from raw dict -> defaults to empty list."""
        raw = {
            "platform": "Myntra",
            "product_title": "Test Dress",
            "product_url": "https://www.myntra.com/test/123",
            "price": 999.0,
            "rating": 4.0,
            "availability": True,
        }
        result = validate_scraped_product(raw)
        assert result is not None
        assert isinstance(result.get("product_details"), list)

    def test_batch_all_details_preserved(self):
        """validate_products preserves product_details across all records."""
        products = [
            _make_valid_raw(["Cotton", "Slim Fit"]),
            _make_valid_raw([]),
            _make_valid_raw(["Polyester"]),
        ]
        validated = validate_products(products)
        assert len(validated) == 3
        assert validated[0]["product_details"] == ["Cotton", "Slim Fit"]
        assert validated[1]["product_details"] == []
        assert validated[2]["product_details"] == ["Polyester"]


# ===========================================================================
# 6. ScrapingService serialization logic
# ===========================================================================

def _compute_details_str(details) -> str:
    """
    Mirrors the fixed logic in scraping_service.py lines 181-188.
    Inline to avoid DB/async dependencies in this unit test.
    """
    if isinstance(details, list) and details:
        return json.dumps(details)
    elif isinstance(details, list):
        return ""
    else:
        return str(details) if details else ""


class TestScrapingServiceDetailsStr:

    def test_non_empty_list_serialized_as_json(self):
        """Non-empty list -> valid JSON array string."""
        result = _compute_details_str(["Cotton", "Regular Fit"])
        assert result.startswith("["), f"Expected JSON array: {result}"
        parsed = json.loads(result)
        assert parsed == ["Cotton", "Regular Fit"]

    def test_empty_list_produces_empty_string_not_brackets(self):
        """Empty list -> '' not '[]'."""
        result = _compute_details_str([])
        assert result == "", f"Expected empty string, got: {repr(result)}"

    def test_none_produces_empty_string(self):
        result = _compute_details_str(None)
        assert result == ""

    def test_empty_string_produces_empty_string(self):
        result = _compute_details_str("")
        assert result == ""

    def test_json_round_trip(self):
        """Serialized JSON is losslessly parseable back to original list."""
        original = ["100% Cotton", "Machine Washable", "Slim Fit", "Colour: Blue"]
        serialized = _compute_details_str(original)
        assert json.loads(serialized) == original
