import json
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.competitor_product import CompetitorProduct
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.user import User
from app.services.pricing_workflow import strategist_node


def _session_with_product(quantity_value=100, quantity_unit="ml"):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(email="pricing@example.test", retailer_name="Test", password_hash="test-hash")
    session.add(user)
    session.flush()
    catalog = ProductCatalog(name="Hibiscus Hair Oil", category="Hair care", brand="Garden", product_details="Hibiscus oil")
    session.add(catalog)
    session.flush()
    product = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id, cost_price=70,
                              stock_quantity=12, minimum_profit_margin=30,
                              quantity_value=quantity_value, quantity_unit=quantity_unit)
    session.add(product)
    session.flush()
    return engine, session, catalog, product


def _competitor(catalog, platform, price, quantity_value=None, quantity_unit=None):
    return CompetitorProduct(catalog_product_id=catalog.id, platform_name=platform,
                             product_name=f"{platform} hair oil", price=price,
                             quantity_value=quantity_value, quantity_unit=quantity_unit)


def test_quantity_comparable_market_range_and_margin_reasoning(monkeypatch):
    engine, db, catalog, product = _session_with_product()
    competitors = [
        _competitor(catalog, "Amazon", 180, 100, "ml"),
        _competitor(catalog, "Flipkart", 250, 200, "ml"),
        _competitor(catalog, "Myntra", 120, 50, "ml"),
    ]
    db.add_all(competitors)
    db.flush()
    prompts = []

    def recommendation(prompt, **kwargs):
        prompts.append(prompt)
        return {"success": True, "data": {"recommended_price": 125, "confidence": 0.8, "warnings": []}}

    monkeypatch.setattr("app.services.llm_service.generate_structured", recommendation)
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                             "competitors": competitors, "evidence_type": "LIVE"})["strategy"]

    assert result["minimum_viable_price"] == 91
    assert result["recommended_price"] == 125
    assert result["expected_margin"] == pytest.approx(78.5714)
    assert result["margin_headroom"] == pytest.approx(48.5714)
    assert result["price_range_low"] == 125
    assert result["price_range_high"] == 240
    assert "100 ml" in result["reasoning_summary"]
    assert "₹91.00" in result["reasoning_summary"]
    assert "₹34.00" in result["reasoning_summary"]
    # Equivalent 100 ml prices are 180, 125, and 240.
    payload = json.loads(prompts[0].split("\n", 1)[1])
    assert [item["price_at_target_pack_size"] for item in payload["competitors"]] == [180, 125, 240]
    db.close()
    engine.dispose()


def test_recommended_price_is_never_below_the_actual_pack_margin_floor(monkeypatch):
    engine, db, catalog, product = _session_with_product()
    competitor = _competitor(catalog, "Amazon", 180, 100, "ml")
    db.add(competitor)
    db.flush()
    monkeypatch.setattr("app.services.llm_service.generate_structured", lambda *args, **kwargs: {
        "success": True, "data": {"recommended_price": 80, "confidence": 0.8, "warnings": []}
    })

    result = strategist_node({"db": db, "retailer_product_id": product.id,
                              "competitors": [competitor], "evidence_type": "LIVE"})["strategy"]

    assert result["minimum_viable_price"] == 91
    assert result["recommended_price"] == 91
    assert result["expected_margin"] == pytest.approx(30)
    assert result["margin_headroom"] == pytest.approx(0)
    assert result["price_range_low"] == 91
    assert result["price_range_high"] == 180
    db.close()
    engine.dispose()


def test_missing_quantity_is_lower_confidence_raw_evidence_not_normalized(monkeypatch):
    engine, db, catalog, product = _session_with_product()
    unknown = _competitor(catalog, "Meesho", 100)
    db.add(unknown)
    db.flush()
    prompts = []

    def recommendation(prompt, **kwargs):
        prompts.append(prompt)
        return {"success": True, "data": {"recommended_price": 110, "confidence": 0.9, "warnings": []}}

    monkeypatch.setattr("app.services.llm_service.generate_structured", recommendation)
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                             "competitors": [unknown], "evidence_type": "LIVE"})["strategy"]
    payload = json.loads(prompts[0].split("\n", 1)[1])

    assert payload["competitors"] == []
    assert payload["raw_market_evidence_without_pack_quantity"][0]["observed_price"] == 100
    assert result["price_range_low"] is None
    assert result["price_range_high"] is None
    assert result["confidence"] == 0.35
    assert "lower-confidence context" in result["reasoning_summary"]
    assert any("lacked pack quantity" in warning for warning in result["warnings"])
    db.close()
    engine.dispose()


def test_incompatible_mass_and_volume_do_not_enter_market_comparison(monkeypatch):
    engine, db, catalog, product = _session_with_product()
    competitor = _competitor(catalog, "Amazon", 200, 100, "g")
    db.add(competitor)
    db.flush()
    prompts = []

    def estimate(prompt, **kwargs):
        prompts.append(prompt)
        return {"success": True, "data": {
            "estimated_price": 110, "price_range_low": 100, "price_range_high": 125,
            "confidence": 0.4, "reasoning": "The cost and minimum margin support this starting price.",
            "evidence_type": "LLM_ESTIMATE",
        }}

    monkeypatch.setattr("app.services.llm_service.generate_structured", estimate)
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                             "competitors": [competitor], "evidence_type": "LIVE"})

    assert result["evidence_type"] == "LLM_ESTIMATE"
    assert result["strategy"]["recommended_price"] >= 91
    assert result["strategy"]["price_range_low"] >= 91
    assert '"observed_price": 200' not in prompts[0]
    db.close()
    engine.dispose()
