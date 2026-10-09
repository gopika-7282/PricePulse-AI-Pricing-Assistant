"""
tests/test_fastapi_scraping_flow.py
====================================
End-to-end FastAPI test verifying:
FastAPI POST /api/products -> product_service -> scraping_service -> ScoutScraper
-> Normalized CompetitorProduct rows stored in database.
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base
from app.services.auth_service import get_db, get_current_user
from app.models.user import User
from app.models.product_catalog import ProductCatalog
from app.models.competitor_product import CompetitorProduct
from app.models.retailer_product import RetailerProduct
from app.services.scraping_service import scrape_product
from app.routes.workflow import _analysis_response

TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestingSession = sessionmaker(bind=TEST_ENGINE, autocommit=False, autoflush=False)
Base.metadata.create_all(bind=TEST_ENGINE)


def override_get_db():
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def setup_user():
    session = TestingSession()
    user = session.query(User).filter_by(id=1).first()
    if not user:
        user = User(id=1, email="test@pricepulse.com", retailer_name="Test Retailer", password_hash="fakehash")
        session.add(user)
        session.commit()
    session.close()


setup_user()


def override_get_current_user():
    session = TestingSession()
    user = session.query(User).filter_by(id=1).first()
    session.close()
    return user


app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_user] = override_get_current_user


def test_product_save_defers_marketplace_refresh_until_analysis():
    client = TestClient(app)

    # Marketplace data is fetched by the explicit pricing analysis action, not
    # by the product-save request.
    mock_platform_statuses = {
        "products": [
            {
                "platform": "Flipkart",
                "product_title": f"Flipkart Product {i}",
                "product_url": f"https://www.flipkart.com/item-{i}",
                "price": 199.0 + i,
                "rating": 4.2,
                "availability": True,
                "product_details": ["Detail A"],
                "scraped_at": "2026-10-02T12:00:00+00:00",
                "ranking": i,
            }
            for i in range(1, 11)
        ] + [
            {
                "platform": "Myntra",
                "product_title": f"Myntra Product {i}",
                "product_url": f"https://www.myntra.com/item-{i}",
                "price": 299.0 + i,
                "rating": 4.1,
                "availability": True,
                "product_details": ["Cotton"],
                "scraped_at": "2026-10-02T12:00:00+00:00",
                "ranking": i,
            }
            for i in range(1, 8)
        ] + [
            {
                "platform": "Meesho",
                "product_title": f"Meesho Product {i}",
                "product_url": f"https://meesho.com/item-{i}",
                "price": 149.0 + i,
                "rating": 3.9,
                "availability": True,
                "product_details": ["Pack of 1"],
                "scraped_at": "2026-10-02T12:00:00+00:00",
                "ranking": i,
            }
            for i in range(1, 11)
        ],
        "platform_statuses": {
            "Flipkart": "OK",
            "Amazon": "BLOCKED",
            "Myntra": "OK",
            "Meesho": "OK",
        },
        "total_products": 27,
        "blocked_platforms": ["Amazon"],
        "failed_platforms": [],
    }

    async def mock_get_platform_statuses(*args, **kwargs):
        return mock_platform_statuses

    with patch("scraping.scout.ScoutScraper.get_platform_statuses", side_effect=mock_get_platform_statuses) as scrape_mock:
        response = client.post(
            "/api/products",
            json={
                "product_name": "Hibiscus Hair Oil",
                "category": "Hair Care",
                "brand": "PricePulse Herbal",
                "product_details": "Pure natural oil",
                "cost_price": 100.0,
                "stock_quantity": 50,
                "minimum_profit_margin": 15.0,
            }
        )

    assert response.status_code == 201

    data = response.json()
    catalog_id = data["catalog_product_id"]

    db = TestingSession()
    competitors = db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == catalog_id).all()
    assert len(competitors) == 0
    scrape_mock.assert_not_awaited()

    catalog_product = db.query(ProductCatalog).filter(ProductCatalog.id == catalog_id).first()
    assert catalog_product.scraping_status == "NEVER_SCRAPED"
    db.close()


def test_multiple_amazon_products_persist_and_api_response_keeps_all(monkeypatch):
    db = TestingSession()
    catalog = ProductCatalog(name="Amazon multi result integration", category="Hair Care", product_details="100 ml")
    db.add(catalog)
    db.commit()
    db.refresh(catalog)

    products = [
        {
            "platform": "Amazon",
            "product_title": f"Hibiscus Hair Oil {i}",
            "product_url": f"https://www.amazon.in/dp/B{i:09d}",
            "price": 100 + i,
            "rating": None,
            "availability": None,
            "product_details": [f"100 ml oil {i}"],
            "scraped_at": "2026-10-08T00:00:00+00:00",
        }
        for i in range(1, 11)
    ]

    async def mock_statuses(self, **kwargs):
        return {
            "products": products,
            "platform_statuses": {"Amazon": "SUCCESS"},
            "blocked_platforms": [],
            "failed_platforms": [],
        }

    monkeypatch.setattr("scraping.scout.ScoutScraper.get_platform_statuses", mock_statuses)
    scrape_product(db, catalog, platforms=["Amazon"], max_products=10)
    persisted = db.query(CompetitorProduct).filter_by(catalog_product_id=catalog.id, platform_name="Amazon").all()
    assert len(persisted) == 10
    assert len({item.product_url for item in persisted}) == 10

    owned = RetailerProduct(
        user_id=1, catalog_product_id=catalog.id, cost_price=70, stock_quantity=5,
        quantity_value=100, quantity_unit="ml", minimum_profit_margin=30,
    )
    db.add(owned)
    db.commit()
    payload = _analysis_response(db, owned, {"competitors": persisted, "marketplace_statuses": {"Amazon": "SUCCESS"}})
    assert len(payload["marketplace_products"]["Amazon"]) == 10
    assert all(row["quantity_display"] == "100 ml" for row in payload["marketplace_products"]["Amazon"])
    db.delete(owned)
    db.delete(catalog)
    db.commit()
    db.close()
