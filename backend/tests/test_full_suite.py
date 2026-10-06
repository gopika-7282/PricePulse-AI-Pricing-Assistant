"""
test_full_suite.py
==================
Comprehensive test suite covering all 26 required scenarios for PricePulse Phase 2.

Scenarios:
SEMANTIC MATCHING (1-9):
1. Turmeric Soap vs Turmeric Soap (Match)
2. Turmeric Soap vs Haldi Soap (Match via Qwen3 multilingual reasoning)
3. Turmeric Soap vs Manjal Soap (Match via Qwen3 with surrounding context)
4. Same product with different brands (Match, brand neutrality)
5. Same product with different category wording (Match, category flexibility)
6. Turmeric Soap vs Turmeric Shampoo (No Match, product-type safety)
7. Turmeric Soap vs Face Wash (No Match, product-type safety)
8. Turmeric Soap vs Neem Soap (No Match, semantic ingredient separation)
9. Turmeric Soap vs Chocolate Cake (No Match, domain and product-type safety)

FRESHNESS (10-14):
10. Competitor data <= 7 days (Fresh -> do not scrape)
11. Competitor data > 7 days (Expired -> scrape required)
12. No competitor data (Scrape required)
13. Failed scrape (Preserve existing data, do not update scraped_at)
14. Expired scrape successfully updates existing competitor

UPSERT (15-18):
15. Same catalog/platform/url does not create duplicate row
16. Existing competitor row is updated in-place
17. Price change creates history (CompetitorPriceHistory entry created)
18. Unchanged price does not create duplicate competitor or price history rows

SCRAPER (19-26):
19. Multiple product extraction (Extracts multiple candidates from search page)
20. Dynamic product URL extraction (Clean normalized URL without tracking garbage)
21. Rating extraction (Validated float strictly in [0.0, 5.0])
22. Details extraction (Specifications, highlights, description)
23. Missing rating handling (Returns None safely without fabrication)
24. Missing details handling (Returns empty list safely without fabrication)
25. Fallback extraction (Recovers when primary selectors/structures mutate)
26. Malformed product card handling (Gracefully skips invalid cards without crashing)
"""

import os
import sys
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure backend is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set SQLite in-memory before importing database models
import app.config as cfg
cfg.DATABASE_URL = "sqlite:///:memory:"

TEST_ENGINE = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=TEST_ENGINE, autocommit=False, autoflush=False)

import app.database as db_module
db_module.engine = TEST_ENGINE
db_module.SessionLocal = TestingSession

from app.database import Base
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory


from app.utils.normalization import (
    normalize_text,
)

from app.services.matching_service import (
    find_catalog_match,
)
from app.services.competitor_service import (
    upsert_competitor_product,
    has_fresh_competitor_data,
    get_competitor_price_history,
    normalize_competitor_url,
)
from app.services.scraping_service import scrape_product
from scraping.parser import (
    parse_search_page,
    parse_detail_page,
    clean_flipkart_url,
    parse_numeric_rating,
    parse_numeric_price,
)
from scraping.validator import validate_product, validate_products


# ── Database Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def init_tables():
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def db():
    connection = TEST_ENGINE.connect()
    transaction = connection.begin()
    session = TestingSession(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ==============================================================================
# PART 1: SEMANTIC MATCHING (Scenarios 1 - 9)
# ==============================================================================

class TestSemanticMatching:

    def test_01_identical_products(self, db):
        """Scenario 1: Turmeric Soap vs Turmeric Soap => MATCH"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Natural herbal turmeric soap 100g"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            details="Natural herbal turmeric soap 100g"
        )
        assert match is not None
        assert match.id == cat.id

    def test_02_haldi_soap_semantic_match(self, db):
        """Scenario 2: Turmeric Soap vs Haldi Soap => MATCH (via Qwen3 multilingual reasoning)"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Natural herbal bathing soap 100g"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Haldi Soap",
            category="Personal Care",
            brand="Himalaya",
            details="Natural herbal bathing soap 100g"
        )
        assert match is not None
        assert match.id == cat.id

    def test_03_manjal_soap_contextual_match(self, db):
        """Scenario 3: Turmeric Soap vs Manjal Soap => MATCH (context supports conclusion)"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Herbal turmeric bathing bar for glowing skin"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Manjal Soap",
            category="Personal Care",
            brand="Aura Beauty",
            details="Pure herbal manjal turmeric bathing bar for glowing skin"
        )
        assert match is not None
        assert match.id == cat.id

    def test_04_different_brands_match(self, db):
        """Scenario 4: Same product with different brands => MATCH (brand neutral)"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Natural turmeric soap"
        )
        db.add(cat)
        db.flush()

        # Incoming has completely different brand "Patanjali"
        match = find_catalog_match(
            db,
            name="Turmeric Soap",
            category="Personal Care",
            brand="Patanjali",
            details="Natural turmeric soap"
        )
        assert match is not None
        assert match.id == cat.id

    def test_05_different_category_wording_match(self, db):
        """Scenario 5: Same product with different category wording => MATCH"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Natural herbal turmeric soap"
        )
        db.add(cat)
        db.flush()

        # Incoming uses "Bath & Body" instead of "Personal Care"
        match = find_catalog_match(
            db,
            name="Turmeric Soap",
            category="Bath & Body",
            brand="Siva's Organic",
            details="Natural herbal turmeric soap"
        )
        assert match is not None
        assert match.id == cat.id

    def test_06_product_type_safety_soap_vs_shampoo(self, db):
        """Scenario 6: Turmeric Soap vs Turmeric Shampoo => NO MATCH (type safety)"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Turmeric bath soap"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Turmeric Shampoo",
            category="Personal Care",
            brand="Siva's Organic",
            details="Turmeric hair shampoo"
        )
        assert match is None

    def test_07_product_type_safety_soap_vs_face_wash(self, db):
        """Scenario 7: Turmeric Soap vs Face Wash => NO MATCH (type safety)"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Turmeric bath soap"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Turmeric Face Wash",
            category="Personal Care",
            brand="Siva's Organic",
            details="Turmeric face cleanser wash"
        )
        assert match is None

    def test_08_ingredient_separation_turmeric_vs_neem(self, db):
        """Scenario 8: Turmeric Soap vs Neem Soap => NO MATCH (different ingredient)"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Pure natural turmeric extract bathing bar"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Neem Soap",
            category="Personal Care",
            brand="Siva's Organic",
            details="Pure natural neem extract bathing bar"
        )
        assert match is None

    def test_09_completely_different_domain(self, db):
        """Scenario 9: Turmeric Soap vs Chocolate Cake => NO MATCH"""
        cat = ProductCatalog(
            name="Turmeric Soap",
            category="Personal Care",
            brand="Siva's Organic",
            product_details="Natural turmeric soap"
        )
        db.add(cat)
        db.flush()

        match = find_catalog_match(
            db,
            name="Chocolate Cake",
            category="Bakery",
            brand="Bakery Delight",
            details="Rich dark chocolate fudge cake"
        )
        assert match is None


# ==============================================================================
# PART 2: 7-DAY FRESHNESS (Scenarios 10 - 14)
# ==============================================================================

class TestFreshness:

    def test_10_fresh_competitor_data_skips_scraping(self, db):
        """Scenario 10: Competitor data <= 7 days old => Fresh, skip scraping"""
        cat = ProductCatalog(name="Test Product", category="Personal Care")
        db.add(cat)
        db.flush()

        comp = CompetitorProduct(
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Test Product",
            product_url="https://www.flipkart.com/test/p/itm123",
            price=299.0,
            scraped_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db.add(comp)
        db.flush()

        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is True

    def test_11_expired_competitor_data_requires_scraping(self, db):
        """Scenario 11: Competitor data > 7 days old => Expired, scrape required"""
        cat = ProductCatalog(name="Test Product", category="Personal Care")
        db.add(cat)
        db.flush()

        comp = CompetitorProduct(
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Test Product",
            product_url="https://www.flipkart.com/test/p/itm123",
            price=299.0,
            scraped_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db.add(comp)
        db.flush()

        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is False

    def test_12_no_competitor_data_requires_scraping(self, db):
        """Scenario 12: No competitor data => Scrape required"""
        cat = ProductCatalog(name="Brand New Product", category="Electronics")
        db.add(cat)
        db.flush()

        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is False

    def test_13_failed_scrape_preserves_existing_data(self, db):
        """Scenario 13: Failed scrape preserves existing data and does not update scraped_at"""
        cat = ProductCatalog(name="Test Soap", category="Personal Care")
        db.add(cat)
        db.flush()

        original_time = datetime.now(timezone.utc) - timedelta(days=9)
        comp = CompetitorProduct(
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Test Soap",
            product_url="https://www.flipkart.com/soap/p/itm1",
            price=150.0,
            scraped_at=original_time,
        )
        db.add(comp)
        db.commit()

        # Simulate failed scraper
        with patch("app.services.scraping_service.ScoutScraper.get_competitor_data", side_effect=Exception("Network error")):
            scrape_product(db, cat)

        db.refresh(comp)
        assert comp.price == 150.0
        # Timestamp must NOT have updated
        comp_time = comp.scraped_at if comp.scraped_at.tzinfo else comp.scraped_at.replace(tzinfo=timezone.utc)
        assert comp_time == original_time
        assert cat.scraping_status == "FAILED"

    def test_14_expired_scrape_successfully_updates_existing_competitor(self, db):
        """Scenario 14: Expired scrape updates existing competitor and refreshes scraped_at"""
        cat = ProductCatalog(name="Test Soap", category="Personal Care")
        db.add(cat)
        db.flush()

        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        comp = CompetitorProduct(
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Test Soap",
            product_url="https://www.flipkart.com/soap/p/itm1",
            price=150.0,
            scraped_at=old_time,
        )
        db.add(comp)
        db.commit()

        # Simulate successful re-scrape with updated price
        mock_data = [{
            "platform_name": "Flipkart",
            "product_name": "Test Soap",
            "product_url": "https://www.flipkart.com/soap/p/itm1",
            "price": 140.0,
            "rating": 4.3,
            "product_details": ["Natural soap"],
            "availability": True,
        }]

        with patch("app.services.scraping_service.ScoutScraper.get_competitor_data", return_value=mock_data):
            scrape_product(db, cat)

        # Verify only 1 competitor row exists (NO duplicate created)
        all_comps = db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == cat.id).all()
        assert len(all_comps) == 1
        updated = all_comps[0]
        assert updated.id == comp.id
        assert updated.price == 140.0
        updated_time = updated.scraped_at if updated.scraped_at.tzinfo else updated.scraped_at.replace(tzinfo=timezone.utc)
        assert updated_time > old_time
        assert cat.scraping_status == "COMPLETED"



# ==============================================================================
# PART 3: COMPETITOR UPSERT & PRICE HISTORY (Scenarios 15 - 18)
# ==============================================================================

class TestCompetitorUpsertAndHistory:

    def test_15_same_identity_does_not_create_duplicate(self, db):
        """Scenario 15: Same (catalog_id, platform, url) does NOT create duplicate row"""
        cat = ProductCatalog(name="Smart Watch", category="Electronics")
        db.add(cat)
        db.flush()

        c1 = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Smart Watch v1",
            price=2499.0,
            product_url="https://www.flipkart.com/watch/p/itm99?pid=WTC123"
        )
        db.flush()

        c2 = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Smart Watch v1 Updated",
            price=2499.0,
            product_url="https://www.flipkart.com/watch/p/itm99?pid=WTC123"
        )
        db.flush()

        assert c1.id == c2.id
        count = db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == cat.id).count()
        assert count == 1

    def test_16_existing_competitor_row_updated_in_place(self, db):
        """Scenario 16: Existing competitor fields are updated in-place"""
        cat = ProductCatalog(name="Facial Scrub", category="Beauty")
        db.add(cat)
        db.flush()

        comp = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Herbal Scrub 50g",
            price=199.0,
            product_url="https://www.flipkart.com/scrub/p/itm44",
            product_details="Old details",
            rating=4.0
        )
        db.flush()

        # Update with new values
        updated = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Herbal Scrub 60g Pack",
            price=189.0,
            product_url="https://www.flipkart.com/scrub/p/itm44",
            product_details="New enriched details",
            rating=4.4
        )
        db.flush()

        assert updated.id == comp.id
        assert updated.product_name == "Herbal Scrub 60g Pack"
        assert updated.price == 189.0
        assert updated.rating == 4.4
        assert updated.product_details == "New enriched details"

    def test_17_price_change_creates_history(self, db):
        """Scenario 17: Price change creates a new CompetitorPriceHistory entry"""
        cat = ProductCatalog(name="Organic Honey", category="Groceries")
        db.add(cat)
        db.flush()

        # Day 1: Price = 250
        comp = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Organic Honey 500g",
            price=250.0,
            product_url="https://www.flipkart.com/honey/p/itm88"
        )
        db.flush()

        h1 = get_competitor_price_history(db, comp.id)
        assert len(h1) == 1
        assert h1[0].price == 250.0

        # Day 8: Price changes to 230
        comp_updated = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Organic Honey 500g",
            price=230.0,
            product_url="https://www.flipkart.com/honey/p/itm88"
        )
        db.flush()

        assert comp_updated.id == comp.id
        assert comp_updated.price == 230.0

        h2 = get_competitor_price_history(db, comp.id)
        assert len(h2) == 2
        prices = [entry.price for entry in h2]
        assert 250.0 in prices
        assert 230.0 in prices

    def test_18_unchanged_price_does_not_create_duplicate_history(self, db):
        """Scenario 18: Unchanged price does not add duplicate price history entries"""
        cat = ProductCatalog(name="Olive Oil", category="Groceries")
        db.add(cat)
        db.flush()

        comp = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Pure Olive Oil 1L",
            price=850.0,
            product_url="https://www.flipkart.com/olive/p/itm77"
        )
        db.flush()

        # Re-scrape with SAME price 850.0
        comp2 = upsert_competitor_product(
            db,
            catalog_product_id=cat.id,
            platform_name="Flipkart",
            product_name="Pure Olive Oil 1L",
            price=850.0,
            product_url="https://www.flipkart.com/olive/p/itm77"
        )
        db.flush()

        assert comp2.id == comp.id
        history = get_competitor_price_history(db, comp.id)
        assert len(history) == 1  # Exactly 1 history entry, no duplication


# ==============================================================================
# PART 4: FLIPKART SCRAPER RESILIENCE & DYNAMICS (Scenarios 19 - 26)
# ==============================================================================

class TestScraperDynamics:

    SAMPLE_SEARCH_HTML = """
    <html>
        <body>
            <div class="search-container">
                <!-- Card 1: Standard layout -->
                <div class="card-item" data-id="SOPH123">
                    <a href="/ayurvedic-turmeric-soap/p/itm111?pid=SOPH111&ssid=abc1" title="Ayurvedic Turmeric Bathing Soap">
                        <img alt="Ayurvedic Turmeric Bathing Soap" src="img1.jpg"/>
                    </a>
                    <div class="price-box">₹199</div>
                    <div class="rating-badge">4.4</div>
                </div>

                <!-- Card 2: Alternative layout (no data-id, anchor holds title) -->
                <div class="custom-card-wrapper">
                    <a href="/himalaya-haldi-soap/p/itm222?pid=SOPH222" class="product-title-link">
                        Himalaya Purifying Neem & Haldi Soap
                    </a>
                    <span class="price-text">₹249</span>
                    <span class="star-rating" aria-label="4.2 out of 5 stars">4.2</span>
                </div>

                <!-- Card 3: Grid card with heading tag -->
                <div class="grid-card">
                    <a href="/natural-manjal-soap/p/itm333?pid=SOPH333">
                        <img alt="Thumbnail" src="img3.jpg"/>
                    </a>
                    <h2>Natural Herbal Manjal Soap 125g</h2>
                    <div>Special Price ₹145</div>
                    <div><span class="badge">4.0</span></div>
                </div>

                <!-- Card 4: Missing rating -->
                <div class="grid-card">
                    <a href="/unrated-soap/p/itm444?pid=SOPH444" title="New Launch Herbal Soap"></a>
                    <div>₹99</div>
                </div>

                <!-- Card 5: Malformed card (no product link) -->
                <div class="broken-card">
                    <div>Ad Banner</div>
                    <div>₹500</div>
                </div>
            </div>
        </body>
    </html>
    """

    SAMPLE_DETAIL_HTML = """
    <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Ayurvedic Turmeric Bathing Soap 100g",
                "description": "Enriched with pure turmeric extract for clear and glowing skin.",
                "offers": {
                    "@type": "Offer",
                    "price": "199.00",
                    "priceCurrency": "INR",
                    "availability": "https://schema.org/InStock"
                },
                "aggregateRating": {
                    "@type": "AggregateRating",
                    "ratingValue": "4.4",
                    "reviewCount": "1200"
                }
            }
            </script>
        </head>
        <body>
            <h1>Ayurvedic Turmeric Bathing Soap 100g</h1>
            <div class="Nx9bqj">₹199</div>
            <div class="XQDdHH">4.4</div>
            <div class="_2418kt">
                <ul>
                    <li>Pure Turmeric & Sandalwood</li>
                    <li>Grade 1 Soap with 76% TFM</li>
                    <li>Cruelty Free & Vegan</li>
                </ul>
            </div>
            <table>
                <tr>
                    <td>Pack of</td>
                    <td>3</td>
                </tr>
                <tr>
                    <td>Skin Type</td>
                    <td>All Skin Types</td>
                </tr>
            </table>
        </body>
    </html>
    """

    def test_19_multiple_product_extraction(self):
        """Scenario 19: Multiple candidate products extracted from search page"""
        products = parse_search_page(self.SAMPLE_SEARCH_HTML, query="Turmeric Soap")
        # 4 valid product cards should be extracted
        assert len(products) == 4
        # Ranking should be sequential
        ranks = [p["ranking"] for p in products]
        assert ranks == [1, 2, 3, 4]

    def test_20_dynamic_product_url_extraction(self):
        """Scenario 20: Clean normalized URL extraction stripped of tracking params"""
        products = parse_search_page(self.SAMPLE_SEARCH_HTML, query="Turmeric Soap")
        url1 = products[0]["product_url"]
        assert "ssid=abc1" not in url1
        assert "pid=SOPH111" in url1
        assert url1.startswith("https://www.flipkart.com/ayurvedic-turmeric-soap/p/itm111")

    def test_21_rating_extraction_validation(self):
        """Scenario 21: Rating correctly extracted and validated in [0.0, 5.0]"""
        products = parse_search_page(self.SAMPLE_SEARCH_HTML, query="Turmeric Soap")
        assert products[0]["rating"] == 4.4
        assert products[1]["rating"] == 4.2
        assert products[2]["rating"] == 4.0

    def test_22_details_extraction(self):
        """Scenario 22: Details extracted from detail page (JSON-LD + highlights + specs)"""
        details = parse_detail_page(self.SAMPLE_DETAIL_HTML, url="https://www.flipkart.com/p/itm111")
        assert details["product_name"] == "Ayurvedic Turmeric Bathing Soap 100g"
        assert details["price"] == 199.0
        assert details["rating"] == 4.4
        assert details["availability"] is True

        dt_list = details["product_details"]
        assert any("Pure Turmeric & Sandalwood" in d for d in dt_list)
        assert any("Pack of: 3" in d for d in dt_list)

    def test_23_missing_rating_handling(self):
        """Scenario 23: Missing rating is safely None, not fabricated"""
        products = parse_search_page(self.SAMPLE_SEARCH_HTML, query="Turmeric Soap")
        # Product 4 has no rating
        assert products[3]["rating"] is None

        # Validator also keeps it None
        validated = validate_product(products[3])
        assert validated["rating"] is None

    def test_24_missing_details_handling(self):
        """Scenario 24: Missing details is safely empty, not fabricated"""
        bare_html = "<html><body><h1>Minimal Soap</h1><span>₹50</span></body></html>"
        details = parse_detail_page(bare_html, url="https://www.flipkart.com/p/itm999")
        assert details["product_name"] == "Minimal Soap"
        assert details["price"] == 50.0
        assert details["product_details"] == []
        assert details["rating"] is None

    def test_25_fallback_extraction_on_mutation(self):
        """Scenario 25: Fallback extraction recovers when primary classes/structure change"""
        mutated_html = """
        <html>
            <title>Mutated Brand Organic Soap - Buy Online</title>
            <body>
                <div class="unknown-header-container">
                    <h2 class="random-class-123">Mutated Brand Organic Soap</h2>
                </div>
                <div class="custom-pricing-area">
                    Price: <span>₹ 349.00</span>
                </div>
                <div class="review-box" aria-label="4.8 stars">
                    Rated 4.8 / 5
                </div>
                <ul>
                    <li>100% Cold Pressed</li>
                    <li>Sulphate Free</li>
                </ul>
            </body>
        </html>
        """
        details = parse_detail_page(mutated_html, url="https://www.flipkart.com/p/itm888")
        assert "Mutated Brand Organic Soap" in details["product_name"]
        assert details["price"] == 349.0
        assert details["rating"] == 4.8
        assert len(details["product_details"]) >= 2

    def test_26_malformed_card_gracefully_skipped(self):
        """Scenario 26: Malformed cards (no link, broken DOM) are skipped without error"""
        malformed_html = """
        <html>
            <body>
                <div class="broken-1"><span>Just text</span></div>
                <div class="broken-2"><a href="#">Empty hash link</a></div>
                <div class="broken-3"><a href="/broken/p/">Broken missing text</a></div>
                <!-- Exactly 1 valid product card -->
                <div class="valid-card">
                    <a href="/valid-soap/p/itm555?pid=SOPH555" title="Valid Handcrafted Soap"></a>
                    <span>₹120</span>
                </div>
            </body>
        </html>
        """
        products = parse_search_page(malformed_html, query="Soap")
        assert len(products) == 1
        assert products[0]["product_name"] == "Valid Handcrafted Soap"
        assert products[0]["price"] == 120.0
