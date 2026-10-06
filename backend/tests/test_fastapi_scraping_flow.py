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


def test_fastapi_multi_site_scraping_flow():
    client = TestClient(app)

    # 4-platform results:
    # Flipkart = 10, Amazon = BLOCKED, Myntra = 7, Meesho = 10 -> Total 27
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

    with patch("scraping.scout.ScoutScraper.get_platform_statuses", side_effect=mock_get_platform_statuses):
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
    assert len(competitors) == 27

    flipkart_items = [c for c in competitors if c.platform_name == "Flipkart"]
    myntra_items = [c for c in competitors if c.platform_name == "Myntra"]
    meesho_items = [c for c in competitors if c.platform_name == "Meesho"]
    amazon_items = [c for c in competitors if c.platform_name == "Amazon"]

    assert len(flipkart_items) == 10
    assert len(myntra_items) == 7
    assert len(meesho_items) == 10
    assert len(amazon_items) == 0

    catalog_product = db.query(ProductCatalog).filter(ProductCatalog.id == catalog_id).first()
    assert catalog_product.scraping_status == "PARTIAL"
    db.close()
