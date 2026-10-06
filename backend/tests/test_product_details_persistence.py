"""
test_product_details_persistence.py
=====================================
Focused regression tests for platform-independent product_details persistence.

Root causes fixed:
  Bug 1: _parse_amazon_detail_page had no generic <ul><li> fallback.
         Flipkart/Myntra/Meesho had it; Amazon silently missed DOM spec items.
  Bug 2: upsert_competitor_product() unconditionally overwrote existing
         product_details with the new value, even when new value was empty.
         A re-scrape with a blocked/failed detail page would erase known specs.

Tests:
  1. Amazon parser now extracts DOM ul/li the same as the other three platforms.
  2. All four platforms carry product_details through the validator unchanged.
  3. scraping_service serialization: [] -> "", non-empty -> JSON string.
  4. upsert (new row): product_details correctly stored.
  5. upsert (existing row): non-empty new details overwrite existing.
  6. upsert (existing row): empty new details do NOT overwrite existing non-empty details.
"""

import json
import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from scraping.parser import parse_detail_page
from scraping.amazon_scraper import _parse_amazon_detail_page
from scraping.myntra_scraper import _parse_myntra_detail_page
from scraping.meesho_scraper import _parse_meesho_detail_page
from scraping.validator import validate_scraped_product, validate_products
from app.services.competitor_service import upsert_competitor_product
from app.models.competitor_product import CompetitorProduct


# ===========================================================================
# Shared HTML factory
# ===========================================================================

def _html_with_jsonld_desc_and_ul(price: str = "499") -> str:
    """HTML with a JSON-LD Product description AND generic <ul><li> spec items."""
    return (
        '<html><body>'
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Test Product",'
        f' "description": "Premium quality item.",'
        f' "offers": {{"price": "{price}", "availability": "InStock"}},'
        ' "aggregateRating": {"ratingValue": "4.2"}}'
        '</script>'
        '<ul><li>100% Cotton</li><li>Machine Washable</li><li>Regular Fit</li></ul>'
        '</body></html>'
    )


def _html_with_ul_only(price: str = "499") -> str:
    """HTML with no JSON-LD description but generic <ul><li> spec items."""
    return (
        '<html><body>'
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Test Product",'
        f' "offers": {{"price": "{price}", "availability": "InStock"}}}}'
        '</script>'
        '<ul><li>Rayon blend</li><li>Dry clean only</li></ul>'
        '</body></html>'
    )


EXAMPLE_URL = "https://example.com/p/test-product-1"

DOM_SPECS = ["100% Cotton", "Machine Washable", "Regular Fit"]


# ===========================================================================
# 1. All four parsers behave identically for generic ul/li
# ===========================================================================

class TestAllParsersExtractDetails:

    def test_flipkart_jsonld_and_dom_accumulated(self):
        result = parse_detail_page(_html_with_jsonld_desc_and_ul(), EXAMPLE_URL)
        details = result.get("product_details", [])
        assert len(details) > 1, f"Expected ≥2 items (JSON-LD + DOM), got: {details}"
        assert any("Cotton" in d for d in details), f"Missing DOM spec: {details}"
        assert any("Premium quality" in d for d in details), f"Missing JSON-LD desc: {details}"

    def test_amazon_jsonld_and_dom_accumulated(self):
        """Amazon was missing generic ul/li -- this test confirms the fix."""
        result = _parse_amazon_detail_page(_html_with_jsonld_desc_and_ul(), EXAMPLE_URL)
        details = result.get("product_details", [])
        assert len(details) > 1, f"Expected ≥2 items (JSON-LD + DOM), got: {details}"
        assert any("Cotton" in d for d in details), f"Amazon missing DOM spec: {details}"
        assert any("Premium quality" in d for d in details), f"Amazon missing JSON-LD desc: {details}"

    def test_myntra_jsonld_and_dom_accumulated(self):
        result = _parse_myntra_detail_page(_html_with_jsonld_desc_and_ul(), EXAMPLE_URL)
        details = result.get("product_details", [])
        assert len(details) > 1, f"Expected ≥2 items, got: {details}"
        assert any("Cotton" in d for d in details), f"Missing DOM spec: {details}"

    def test_meesho_jsonld_and_dom_accumulated(self):
        result = _parse_meesho_detail_page(_html_with_jsonld_desc_and_ul(), EXAMPLE_URL)
        details = result.get("product_details", [])
        assert len(details) > 1, f"Expected ≥2 items, got: {details}"
        assert any("Cotton" in d for d in details), f"Missing DOM spec: {details}"

    def test_all_four_same_dom_only(self):
        """All four parsers extract the same DOM items when JSON-LD has no description."""
        results = {
            "Flipkart": parse_detail_page(_html_with_ul_only(), EXAMPLE_URL).get("product_details", []),
            "Amazon":   _parse_amazon_detail_page(_html_with_ul_only(), EXAMPLE_URL).get("product_details", []),
            "Myntra":   _parse_myntra_detail_page(_html_with_ul_only(), EXAMPLE_URL).get("product_details", []),
            "Meesho":   _parse_meesho_detail_page(_html_with_ul_only(), EXAMPLE_URL).get("product_details", []),
        }
        for platform, details in results.items():
            assert len(details) > 0, f"{platform} returned no details: {details}"
            assert any("Rayon" in d for d in details), f"{platform} missing DOM spec: {details}"

    def test_no_duplicates_any_parser(self):
        """No parser creates duplicate detail strings."""
        for parser, name in [
            (lambda h: parse_detail_page(h, EXAMPLE_URL), "Flipkart"),
            (lambda h: _parse_amazon_detail_page(h, EXAMPLE_URL), "Amazon"),
            (lambda h: _parse_myntra_detail_page(h, EXAMPLE_URL), "Myntra"),
            (lambda h: _parse_meesho_detail_page(h, EXAMPLE_URL), "Meesho"),
        ]:
            result = parser(_html_with_jsonld_desc_and_ul())
            details = result.get("product_details", [])
            assert len(details) == len(set(details)), f"{name} has duplicates: {details}"


# ===========================================================================
# 2. Validator preserves product_details for all platforms
# ===========================================================================

def _make_validated(platform: str, details: List[str]) -> Optional[Dict[str, Any]]:
    return validate_scraped_product({
        "platform": platform,
        "product_title": "Test Product",
        "product_url": "https://example.com/p/test-product-1",
        "price": 499.0,
        "rating": 4.2,
        "availability": True,
        "product_details": details,
        "scraped_at": "2025-01-01T00:00:00+00:00",
    })


class TestValidatorPlatformIndependent:

    def test_flipkart_details_pass_through(self):
        v = _make_validated("Flipkart", DOM_SPECS)
        assert v is not None and v["product_details"] == DOM_SPECS

    def test_amazon_details_pass_through(self):
        v = _make_validated("Amazon", DOM_SPECS)
        assert v is not None and v["product_details"] == DOM_SPECS

    def test_myntra_details_pass_through(self):
        v = _make_validated("Myntra", DOM_SPECS)
        assert v is not None and v["product_details"] == DOM_SPECS

    def test_meesho_details_pass_through(self):
        v = _make_validated("Meesho", DOM_SPECS)
        assert v is not None and v["product_details"] == DOM_SPECS


# ===========================================================================
# 3. Serialization (scraping_service.py logic)
# ===========================================================================

def _details_to_str(details) -> str:
    """Mirrors scraping_service.py lines 181-188 (post-fix)."""
    if isinstance(details, list) and details:
        return json.dumps(details)
    elif isinstance(details, list):
        return ""
    else:
        return str(details) if details else ""


class TestDetailsSerialisation:

    def test_non_empty_list_to_json_string(self):
        result = _details_to_str(DOM_SPECS)
        assert result.startswith("[")
        assert json.loads(result) == DOM_SPECS

    def test_empty_list_to_empty_string(self):
        """Empty list must not become '[]'."""
        assert _details_to_str([]) == ""

    def test_none_to_empty_string(self):
        assert _details_to_str(None) == ""


# ===========================================================================
# 4. upsert_competitor_product() — persistence path
# ===========================================================================

def _make_mock_db(existing: Optional[CompetitorProduct] = None):
    """Create a mock SQLAlchemy session that returns `existing` on query."""
    db = MagicMock()
    query_mock = MagicMock()
    db.query.return_value = query_mock
    query_mock.filter.return_value = query_mock
    query_mock.first.return_value = existing
    query_mock.all.return_value = [existing] if existing else []
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


class TestUpsertProductDetails:

    def test_new_row_stores_product_details(self):
        """New competitor row gets product_details from the scraper."""
        db = _make_mock_db(existing=None)
        result = upsert_competitor_product(
            db=db,
            catalog_product_id=1,
            platform_name="Flipkart",
            product_name="Test Product",
            price=499.0,
            product_url="https://www.flipkart.com/test/p/abc123",
            product_details='["100% Cotton", "Machine Washable"]',
            rating=4.2,
            availability=True,
        )
        assert db.add.called, "db.add() should be called for a new row"
        assert result.product_details == '["100% Cotton", "Machine Washable"]'

    def test_existing_row_updated_with_non_empty_details(self):
        """Existing competitor row gets its product_details updated when new value is non-empty."""
        existing = CompetitorProduct(
            id=42,
            catalog_product_id=1,
            platform_name="Amazon",
            product_name="Old Name",
            product_url="https://www.amazon.in/dp/B001",
            product_details="",           # existing has no details
            price=450.0,
            rating=4.0,
            availability=True,
            scraped_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db = _make_mock_db(existing=existing)

        upsert_competitor_product(
            db=db,
            catalog_product_id=1,
            platform_name="Amazon",
            product_name="Updated Name",
            price=499.0,
            product_url="https://www.amazon.in/dp/B001",
            product_details='["Feature A", "Feature B"]',
            rating=4.2,
            availability=True,
        )
        assert existing.product_details == '["Feature A", "Feature B"]', (
            f"Expected updated details, got: {existing.product_details}"
        )

    def test_existing_row_not_overwritten_when_new_details_empty(self):
        """
        Existing non-empty product_details MUST NOT be overwritten when
        the new scrape provides empty/None details (e.g. detail page blocked).
        This is the upsert Bug 2 fix.
        """
        existing = CompetitorProduct(
            id=43,
            catalog_product_id=1,
            platform_name="Myntra",
            product_name="Myntra Dress",
            product_url="https://www.myntra.com/product/123",
            product_details='["Polyester blend", "Dry clean only"]',  # existing has details
            price=999.0,
            rating=4.1,
            availability=True,
            scraped_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db = _make_mock_db(existing=existing)

        upsert_competitor_product(
            db=db,
            catalog_product_id=1,
            platform_name="Myntra",
            product_name="Myntra Dress",
            price=1099.0,           # price changed
            product_url="https://www.myntra.com/product/123",
            product_details="",     # detail page failed -- empty string
            rating=4.1,
            availability=True,
        )
        assert existing.product_details == '["Polyester blend", "Dry clean only"]', (
            f"Existing details should NOT be overwritten by empty. Got: {existing.product_details}"
        )

    def test_existing_row_not_overwritten_when_new_details_none(self):
        """Same as above but new product_details=None."""
        existing = CompetitorProduct(
            id=44,
            catalog_product_id=1,
            platform_name="Meesho",
            product_name="Meesho Kurti",
            product_url="https://meesho.com/kurti/p/456",
            product_details='["Cotton fabric", "Hand wash"]',
            price=399.0,
            rating=4.0,
            availability=True,
            scraped_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db = _make_mock_db(existing=existing)

        upsert_competitor_product(
            db=db,
            catalog_product_id=1,
            platform_name="Meesho",
            product_name="Meesho Kurti",
            price=449.0,
            product_url="https://meesho.com/kurti/p/456",
            product_details=None,   # detail page gave nothing
            rating=4.0,
            availability=True,
        )
        assert existing.product_details == '["Cotton fabric", "Hand wash"]', (
            f"Existing details should NOT be overwritten by None. Got: {existing.product_details}"
        )

    def test_all_four_platforms_produce_details_through_full_pipeline(self):
        """
        End-to-end mock test: simulate each platform returning a product with
        product_details, verify validate_products passes them through, and
        verify the serialization produces a non-empty JSON string.
        """
        raw_products = [
            {
                "platform": "Flipkart",
                "product_title": "Hibiscus Hair Oil Flipkart",
                "product_url": "https://www.flipkart.com/hibiscus/p/abc1",
                "price": 299.0,
                "rating": 4.3,
                "availability": True,
                "product_details": ["Natural herbal oil", "100 ml bottle"],
                "scraped_at": "2025-01-01T00:00:00+00:00",
            },
            {
                "platform": "Amazon",
                "product_title": "Hibiscus Hair Oil Amazon",
                "product_url": "https://www.amazon.in/dp/B123",
                "price": 319.0,
                "rating": 4.1,
                "availability": True,
                "product_details": ["Natural herbal oil", "100 ml bottle"],
                "scraped_at": "2025-01-01T00:00:00+00:00",
            },
            {
                "platform": "Myntra",
                "product_title": "Hibiscus Hair Oil Myntra",
                "product_url": "https://www.myntra.com/hair-oil/123",
                "price": 279.0,
                "rating": 4.0,
                "availability": True,
                "product_details": ["Natural herbal oil", "100 ml bottle"],
                "scraped_at": "2025-01-01T00:00:00+00:00",
            },
            {
                "platform": "Meesho",
                "product_title": "Hibiscus Hair Oil Meesho",
                "product_url": "https://meesho.com/oil/p/456",
                "price": 249.0,
                "rating": 3.9,
                "availability": True,
                "product_details": ["Natural herbal oil", "100 ml bottle"],
                "scraped_at": "2025-01-01T00:00:00+00:00",
            },
        ]

        validated = validate_products(raw_products)
        assert len(validated) == 4, f"All 4 records should pass validation, got {len(validated)}"

        for product in validated:
            platform = product["platform"]
            details = product["product_details"]
            assert isinstance(details, list), f"{platform}: product_details should be a list"
            assert len(details) > 0, f"{platform}: product_details should be non-empty"
            assert "Natural herbal oil" in details, f"{platform}: expected detail missing: {details}"

            # Serialization step (mirrors scraping_service.py)
            details_str = _details_to_str(details)
            assert details_str, f"{platform}: serialized details_str should be non-empty"
            assert details_str != "[]", f"{platform}: should not store '[]'"
            deserialized = json.loads(details_str)
            assert deserialized == details, f"{platform}: JSON round-trip failed"
