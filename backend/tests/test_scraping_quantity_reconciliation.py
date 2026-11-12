from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.competitor_product import CompetitorProduct
from app.models.product_catalog import ProductCatalog
from app.services import scraping_service
from app.utils.quantity import quantity_is_uncertain


def test_scrape_persists_conflicting_title_and_scraper_quantity_as_uncertain(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    catalog = ProductCatalog(name="Aloe Vera Gel", category="Skin care", product_details="Aloe vera gel")
    db.add(catalog)
    db.flush()

    class StubScout:
        def get_platform_statuses(self, **kwargs):
            return {
                "products": [{
                    "platform": "Amazon", "product_title": "Aloe Vera Gel 100 g pack of 3",
                    "product_url": "https://example.test/aloe-pack", "price": 300,
                    "quantity_value": 100, "quantity_unit": "g", "pack_count": 1,
                    "total_quantity": 100, "total_quantity_unit": "g",
                    "scraped_at": "2026-01-01T00:00:00+00:00",
                    "product_details": ["Aloe vera gel"],
                }],
                "platform_statuses": {"Amazon": "OK"},
                "blocked_platforms": [], "failed_platforms": [],
            }

    monkeypatch.setattr(scraping_service, "ScoutScraper", StubScout)
    scraping_service.scrape_product(db, catalog)
    row = db.query(CompetitorProduct).one()
    assert row.quantity_value is None
    assert row.total_quantity is None
    assert quantity_is_uncertain(row.product_details)
    assert '"source":"title"' in row.product_details
    assert '"source":"scraper_metadata"' in row.product_details
    db.close()
    engine.dispose()
