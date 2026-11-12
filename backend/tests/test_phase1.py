"""
Phase 1 comprehensive test suite for PricePulse.

Tests all 17 required scenarios using in-memory SQLite so no Postgres
connection is needed for unit tests.

Run with:
  cd E:\\GOPIKA\\PrintPulse\\backend
  ..\\venv\\Scripts\\python.exe -m pytest tests/test_phase1.py -v
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── Bootstrap in-memory DB before any app imports that touch DATABASE_URL ──
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Patch DATABASE_URL so app.database connects to SQLite in-memory
import app.config as cfg
cfg.DATABASE_URL = "sqlite:///:memory:"

from app.database import Base, engine as _real_engine

TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False}
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autocommit=False, autoflush=False)

# Override the module-level engine before models are imported
import app.database as db_module
db_module.engine = TEST_ENGINE
db_module.SessionLocal = TestingSession

from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory

from app.utils.normalization import normalize_text
from app.services.product_service import (
    find_existing_product,
    create_product_catalog,
)
from app.services.competitor_service import (
    upsert_competitor_product,
    create_manual_competitor_product,
    update_competitor_price,
    has_fresh_competitor_data,
    get_competitor_price_history,
    save_price_history,
)
from app.services.scraping_service import scrape_product


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def create_tables():
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture
def db():
    """Fresh transactional session that rolls back after each test."""
    connection = TEST_ENGINE.connect()
    transaction = connection.begin()
    session = TestingSession(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ── Helper ───────────────────────────────────────────────────────────────────

def make_catalog(db, name, category="Personal Care", brand="TestBrand", details=""):
    c = ProductCatalog(name=name, category=category, brand=brand, product_details=details)
    db.add(c)
    db.flush()
    return c


def make_competitor(db, catalog_id, platform="Flipkart", name="Test", price=100.0,
                    url="http://example.com/p/1", scraped_at=None):
    now = scraped_at or datetime.now(timezone.utc)
    c = CompetitorProduct(
        catalog_product_id=catalog_id,
        platform_name=platform,
        product_name=name,
        price=price,
        product_url=url,
        scraped_at=now,
        observation_source="SCRAPE",
    )
    db.add(c)
    db.flush()
    return c


def make_async_scout_scraper(products):
    """Model ScoutScraper's async status API and spy on its awaited call."""
    status_result = {
        "products": products,
        "platform_statuses": {"Flipkart": "OK"},
        "blocked_platforms": [],
        "failed_platforms": [],
    }
    status_call = AsyncMock(return_value=status_result)

    class AsyncScoutScraper:
        async def get_competitor_data(self, **kwargs):
            raise AssertionError("The service should use get_platform_statuses")

        async def get_platform_statuses(self, **kwargs):
            return await status_call(**kwargs)

    return AsyncScoutScraper(), status_call


# ═══════════════════════════════════════════════════════════════════════════
# PART A — Semantic Normalization
# ═══════════════════════════════════════════════════════════════════════════

class TestSemanticNormalization:

    def test_apostrophe_removed(self):
        assert "'" not in normalize_text("Siva's Organic")


# ═══════════════════════════════════════════════════════════════════════════
# PART B — ProductCatalog Matching
# ═══════════════════════════════════════════════════════════════════════════

class TestCatalogMatching:

    # T1 — Exact product match
    def test_exact_product_match(self, db):
        catalog = make_catalog(db, "Turmeric Soap", brand="Himalaya")
        with patch("app.services.product_identity_service.generate_structured", return_value={"success": True, "data": {"decision": "MATCH", "matched_catalog_id": catalog.id, "confidence": 1.0, "reason": "Mocked identity response."}}):
            result = find_existing_product(db, "Turmeric Soap", "Personal Care", "Himalaya")
        assert result is not None, "Exact match should return existing catalog"

    # T2 — Turmeric vs Haldi (semantic match)
    def test_turmeric_matches_haldi(self, db):
        make_catalog(db, "Turmeric Soap", brand="Himalaya", category="Personal Care")
        catalog = db.query(ProductCatalog).filter_by(name="Turmeric Soap").first()
        with patch("app.services.product_identity_service.generate_structured", return_value={"success": True, "data": {"decision": "MATCH", "matched_catalog_id": catalog.id, "confidence": 0.94, "reason": "Generic identity confirmed from the product evidence."}}):
            result = find_existing_product(db, "Haldi Soap", "Personal Care", "Patanjali")
        assert result is not None, "Haldi Soap should match Turmeric Soap after semantic normalization"

    # T3 — Turmeric vs Manjal (semantic match)
    def test_turmeric_matches_manjal(self, db):
        make_catalog(db, "Turmeric Soap", brand="Himalaya", category="Personal Care")
        catalog = db.query(ProductCatalog).filter_by(name="Turmeric Soap").first()
        with patch("app.services.product_identity_service.generate_structured", return_value={"success": True, "data": {"decision": "MATCH", "matched_catalog_id": catalog.id, "confidence": 0.93, "reason": "Generic identity confirmed from the product evidence."}}):
            result = find_existing_product(db, "Manjal Soap", "Personal Care", "Medimix")
        assert result is not None, "Manjal Soap should match Turmeric Soap after semantic normalization"

    # T4 — Same product, different brands → MATCH
    def test_different_brands_same_product_matches(self, db):
        catalog = make_catalog(db, "Turmeric Soap", brand="Siva's Organic", category="Personal Care")
        with patch("app.services.product_identity_service.generate_structured", return_value={"success": True, "data": {"decision": "MATCH", "matched_catalog_id": catalog.id, "confidence": 1.0, "reason": "Mocked identity response."}}):
            result = find_existing_product(db, "Turmeric Soap", "Personal Care", "Himalaya")
        assert result is not None, "Different brands must NOT prevent catalog reuse"

    # T5 — Different product type → NO MATCH
    def test_soap_vs_shampoo_no_match(self, db):
        make_catalog(db, "Turmeric Soap", brand="Himalaya", category="Personal Care")
        result = find_existing_product(db, "Turmeric Shampoo", "Personal Care", "Himalaya")
        assert result is None, "Turmeric Soap vs Turmeric Shampoo must NOT match"

    # T6 — Different product characteristic → NO MATCH
    def test_different_main_ingredient_no_match(self, db):
        make_catalog(db, "Neem Soap", brand="Himalaya", category="Personal Care")
        result = find_existing_product(db, "Turmeric Soap", "Personal Care", "Himalaya")
        assert result is None, "Neem Soap vs Turmeric Soap should NOT match"

    # T7 — Unrelated product → NO MATCH
    def test_unrelated_product_no_match(self, db):
        make_catalog(db, "Turmeric Soap", brand="Himalaya", category="Personal Care")
        result = find_existing_product(db, "Chocolate Cake", "Bakery", "Generic")
        assert result is None, "Completely unrelated product must NOT match"

    # T6-extra — Turmeric Soap vs Turmeric Face Wash → NO MATCH
    def test_soap_vs_face_wash_no_match(self, db):
        make_catalog(db, "Turmeric Soap", brand="Himalaya", category="Personal Care")
        result = find_existing_product(db, "Turmeric Face Wash", "Personal Care", "Himalaya")
        assert result is None, "Turmeric Soap vs Turmeric Face Wash must NOT match"

    # T_extra — Turmeric Soap vs Turmeric Herbal Soap → MATCH
    def test_turmeric_herbal_soap_matches(self, db):
        catalog = make_catalog(db, "Turmeric Soap", brand="Himalaya", category="Personal Care")
        with patch("app.services.product_identity_service.generate_structured", return_value={"success": True, "data": {"decision": "MATCH", "matched_catalog_id": catalog.id, "confidence": 1.0, "reason": "Mocked identity response."}}):
            result = find_existing_product(db, "Turmeric Herbal Soap", "Personal Care", "Himalaya")
        assert result is not None, "Turmeric Herbal Soap should match Turmeric Soap"


# ═══════════════════════════════════════════════════════════════════════════
# PART C — Freshness Logic
# ═══════════════════════════════════════════════════════════════════════════

class TestFreshness:

    # T8 — Fresh competitor data (≤ 7 days) → do NOT scrape
    def test_fresh_data_returns_true(self, db):
        cat = make_catalog(db, "Fresh Soap")
        make_competitor(db, cat.id, scraped_at=datetime.now(timezone.utc) - timedelta(days=2))
        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is True

    # T9 — Stale competitor data (> 7 days) → scrape
    def test_stale_data_returns_false(self, db):
        cat = make_catalog(db, "Stale Soap")
        make_competitor(db, cat.id, scraped_at=datetime.now(timezone.utc) - timedelta(days=10))
        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is False

    def test_no_competitor_data_returns_false(self, db):
        cat = make_catalog(db, "New Soap")
        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is False

    def test_exactly_7_days_is_fresh(self, db):
        cat = make_catalog(db, "Seven Day Soap")
        make_competitor(db, cat.id, scraped_at=datetime.now(timezone.utc) - timedelta(days=7))
        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is True


# ═══════════════════════════════════════════════════════════════════════════
# PART D — Competitor Upsert
# ═══════════════════════════════════════════════════════════════════════════

class TestCompetitorUpsert:

    # T10 — Re-scraping same URL must UPDATE, not INSERT new row
    def test_upsert_updates_existing_by_url(self, db):
        cat = make_catalog(db, "Upsert Soap")
        url = "https://flipkart.com/p/abc123"

        comp1 = upsert_competitor_product(
            db, cat.id, "Flipkart", "Upsert Soap v1", 100.0, url, None, 4.0, True,
            scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE"
        )
        db.flush()
        first_id = comp1.id

        comp2 = upsert_competitor_product(
            db, cat.id, "Flipkart", "Upsert Soap v2", 120.0, url, None, 4.2, True,
            scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE"
        )
        db.flush()

        count = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).count()
        assert count == 1, "Must NOT create a duplicate competitor row"
        assert comp2.id == first_id, "Updated row must have same id"
        assert comp2.price == 120.0, "Price must be updated"

    # T11 — Price history is preserved correctly
    def test_price_history_preserved_on_update(self, db):
        cat = make_catalog(db, "History Soap")
        url = "https://flipkart.com/p/hist001"

        upsert_competitor_product(db, cat.id, "Flipkart", "History Soap", 100.0, url,
                                  scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE")
        db.flush()
        upsert_competitor_product(db, cat.id, "Flipkart", "History Soap", 130.0, url,
                                  scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE")
        db.flush()

        comp = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).first()
        history = get_competitor_price_history(db, comp.id)
        prices = [h.price for h in history]

        assert 100.0 in prices, "Original price must be in history"
        assert 130.0 in prices, "New price must be in history"
        assert len(history) == 2, "Must have exactly 2 history entries for 2 price changes"

    def test_no_duplicate_history_when_price_unchanged(self, db):
        cat = make_catalog(db, "Same Price Soap")
        url = "https://flipkart.com/p/same001"

        upsert_competitor_product(db, cat.id, "Flipkart", "Same Soap", 99.0, url,
                                  scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE")
        db.flush()
        upsert_competitor_product(db, cat.id, "Flipkart", "Same Soap", 99.0, url,
                                  scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE")
        db.flush()

        comp = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).first()
        history = get_competitor_price_history(db, comp.id)
        assert len(history) == 1, "Price unchanged — only 1 history entry expected"

    def test_new_product_always_gets_history(self, db):
        cat = make_catalog(db, "Brand New Soap")
        upsert_competitor_product(db, cat.id, "Flipkart", "New Soap", 50.0,
                                  scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE")
        db.flush()

        comp = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).first()
        history = get_competitor_price_history(db, comp.id)
        assert len(history) == 1, "New competitor must have 1 history entry"

    def test_manual_competitor_price_is_not_scrape_freshness_or_history(self, db):
        cat = make_catalog(db, "Manual Price Soap")
        manual = create_manual_competitor_product(
            db, catalog_product_id=cat.id, platform_name="Manual",
            product_name="Store entered listing", price=85.0,
        )
        db.flush()
        assert manual.observation_source == "MANUAL"
        assert manual.scraped_at is not None  # storage default is not provenance
        assert get_competitor_price_history(db, manual.id) == []
        assert has_fresh_competitor_data(db, cat.id, threshold_days=7) is False

        update_competitor_price(db, manual.id, 90.0)
        assert manual.price == 90.0
        assert manual.scraped_at is not None
        assert get_competitor_price_history(db, manual.id) == []


# ═══════════════════════════════════════════════════════════════════════════
# PART E — Scraping Service (mocked scraper)
# ═══════════════════════════════════════════════════════════════════════════

class TestScrapingService:

    # T12 — Scraper failure: scraped_at must NOT be updated
    def test_scraper_failure_does_not_update_scraped_at(self, db):
        cat = make_catalog(db, "Fail Soap")

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            MockScraper.return_value.get_competitor_data = AsyncMock(
                side_effect=Exception("Network timeout")
            )
            scrape_product(db, cat)

        db.expire(cat)
        db.refresh(cat)
        assert cat.last_scraped_at is None, "scraped_at must not be set on scraper failure"
        assert cat.scraping_status == "FAILED"

    def test_missing_observation_timestamp_is_rejected_and_not_persisted(self, db):
        from scraping.validator import validate_scraped_product

        raw = {
            "platform_name": "Flipkart", "product_name": "Untimed Soap",
            "price": 45.0, "product_url": "https://flipkart.com/p/untimed",
            "rating": None, "availability": True,
        }
        assert validate_scraped_product(raw) is None
        for untrusted_timestamp in ("not-a-timestamp", "2026-11-12T10:00:00"):
            assert validate_scraped_product({**raw, "scraped_at": untrusted_timestamp}) is None

        cat = make_catalog(db, "Untimed Soap")
        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper([raw])
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        assert db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).count() == 0
        assert db.query(CompetitorPriceHistory).count() == 0

    def test_async_scraper_result_is_awaited_and_timestamp_preserved(self, db):
        observed_at = "2026-11-11T08:15:30+00:00"
        from scraping.validator import validate_scraped_product

        raw = {
            "platform_name": "Flipkart", "product_name": "Timed Soap",
            "price": 45.0, "product_url": "https://flipkart.com/p/timed",
            "rating": None, "availability": True, "scraped_at": observed_at,
        }
        validated = validate_scraped_product(raw)
        assert validated["scraped_at"] == observed_at

        cat = make_catalog(db, "Timed Soap")
        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper([raw])
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        competitor = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).one()
        history = db.query(CompetitorPriceHistory).filter_by(competitor_product_id=competitor.id).one()
        expected_at = datetime.fromisoformat(observed_at).replace(tzinfo=None)
        # SQLite drops timezone metadata; the persisted UTC wall time must match.
        assert competitor.scraped_at == expected_at
        assert history.scraped_at == expected_at

    def test_async_scraper_missing_rating_remains_none(self, db):
        cat = make_catalog(db, "Unrated Soap")
        result = [{
            "platform_name": "Flipkart", "product_name": "Unrated Soap",
            "price": 52.0, "product_url": "https://flipkart.com/p/unrated",
            "rating": None, "availability": True,
            "scraped_at": "2026-11-12T10:00:00+00:00",
        }]
        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper(result)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        competitor = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).one()
        assert competitor.rating is None

    # T17 — Multiple Flipkart products returned
    def test_multiple_flipkart_products_stored(self, db):
        cat = make_catalog(db, "Multi Soap")

        fake_results = [
            {
                "platform_name": "Flipkart",
                "product_name": "Multi Soap A",
                "price": 50.0,
                "product_url": "https://flipkart.com/p/A",
                "product_details": ["200ml"],
                "rating": 4.1,
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            },
            {
                "platform_name": "Flipkart",
                "product_name": "Multi Soap B",
                "price": 60.0,
                "product_url": "https://flipkart.com/p/B",
                "product_details": ["100ml"],
                "rating": 3.8,
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            },
        ]

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper(fake_results)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        competitors = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).order_by(CompetitorProduct.product_name).all()
        assert len(competitors) == 2
        assert [item.rating for item in competitors] == [4.1, 3.8]
        assert db.query(CompetitorPriceHistory).count() == 2

    # T13 — DB persistence failure rolls back, no competitor rows left
    def test_db_persist_failure_rolls_back(self, db):
        cat = make_catalog(db, "Rollback Soap")

        fake_results = [
            {
                "platform_name": "Flipkart",
                "product_name": "Rollback Soap",
                "price": 80.0,
                "product_url": "https://flipkart.com/p/rollback",
                "product_details": [],
                "rating": None,
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            }
        ]

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper, \
             patch("app.services.scraping_service.upsert_competitor_product",
                   side_effect=Exception("DB constraint error")):
            scraper, status_call = make_async_scout_scraper(fake_results)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        count = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).count()
        assert count == 0, "On DB failure, no competitor rows should be persisted"

    # T14 — Recommendation failure must NOT erase committed competitor data  
    def test_recommendation_failure_preserves_competitor_data(self, db):
        """
        Competitor data is committed by scrape_product before recommendations run.
        If recommendations fail, those rows must remain.
        """
        cat = make_catalog(db, "Rec Fail Soap")

        fake_results = [
            {
                "platform_name": "Flipkart",
                "product_name": "Rec Fail Soap",
                "price": 75.0,
                "product_url": "https://flipkart.com/p/rec",
                "product_details": ["Net 200ml"],
                "rating": 4.0,
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            }
        ]

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper(fake_results)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        # The scraper is a separate persistence boundary from LangGraph's
        # Strategist. Fail during its real pricing calculation after scraping
        # has committed the competitor row.
        from app.services.pricing_workflow import strategist_node
        retailer_product = RetailerProduct(
            user_id=1, catalog_product_id=cat.id, cost_price=50.0,
            stock_quantity=2, minimum_profit_margin=20.0,
        )
        db.add(retailer_product)
        db.flush()
        competitors = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).all()
        with patch("app.services.pricing_service.minimum_viable_selling_price",
                   side_effect=RuntimeError("Recommendation boom")):
            with pytest.raises(RuntimeError, match="Recommendation boom"):
                strategist_node({
                    "db": db, "retailer_product_id": retailer_product.id,
                    "competitors": competitors, "evidence_type": "LIVE",
                })
        count = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).count()
        assert count == 1, "Competitor data must survive downstream recommendation failure"

    # T15 — Rating extracted correctly (valid 0-5)
    def test_rating_extracted_valid(self, db):
        cat = make_catalog(db, "Rated Soap")

        fake_results = [
            {
                "platform_name": "Flipkart",
                "product_name": "Rated Soap",
                "price": 90.0,
                "product_url": "https://flipkart.com/p/rated",
                "product_details": [],
                "rating": 4.3,
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            }
        ]

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper(fake_results)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        comp = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).first()
        assert comp is not None
        assert comp.rating == 4.3

    def test_invalid_rating_becomes_null(self, db):
        cat = make_catalog(db, "Bad Rated Soap")

        fake_results = [
            {
                "platform_name": "Flipkart",
                "product_name": "Bad Rated Soap",
                "price": 90.0,
                "product_url": "https://flipkart.com/p/badrated",
                "product_details": [],
                "rating": 7.5,  # Invalid — > 5; scraper should have set to None before calling service
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            }
        ]

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper(fake_results)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        comp = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).first()
        assert comp is not None
        assert comp.rating is None

    # T16 — Product details extracted and stored as JSON
    def test_product_details_extracted(self, db):
        cat = make_catalog(db, "Details Soap")
        details_list = ["200ml pack", "Herbal ingredients", "No parabens"]

        fake_results = [
            {
                "platform_name": "Flipkart",
                "product_name": "Details Soap",
                "price": 85.0,
                "product_url": "https://flipkart.com/p/details",
                "product_details": details_list,
                "rating": 4.0,
                "availability": True,
                "scraped_at": "2026-11-12T10:00:00+00:00",
            }
        ]

        with patch("app.services.scraping_service.ScoutScraper") as MockScraper:
            scraper, status_call = make_async_scout_scraper(fake_results)
            MockScraper.return_value = scraper
            scrape_product(db, cat)
            status_call.assert_awaited_once()

        comp = db.query(CompetitorProduct).filter_by(catalog_product_id=cat.id).first()
        assert comp is not None
        from app.utils.quantity import strip_quantity_marker
        from app.services.relevance_filter_service import strip_match_marker
        assert "[[PRICEPULSE_QUANTITY_EVIDENCE:" in comp.product_details
        stored = json.loads(strip_match_marker(strip_quantity_marker(comp.product_details)))
        assert stored == details_list


# ═══════════════════════════════════════════════════════════════════════════
# PART F — Schema Verification (no seller_name / review_count)
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaCleanup:

    def test_competitor_product_has_no_seller_name(self):
        cols = [c.key for c in CompetitorProduct.__table__.columns]
        assert "seller_name" not in cols

    def test_competitor_product_has_no_review_count(self):
        cols = [c.key for c in CompetitorProduct.__table__.columns]
        assert "review_count" not in cols

    def test_competitor_product_has_expected_columns(self):
        cols = [c.key for c in CompetitorProduct.__table__.columns]
        for expected in ["id", "catalog_product_id", "platform_name", "product_name",
                          "product_details", "price", "rating", "availability",
                          "scraped_at", "created_at", "updated_at"]:
            assert expected in cols, f"Expected column '{expected}' not found"


# ═══════════════════════════════════════════════════════════════════════════

