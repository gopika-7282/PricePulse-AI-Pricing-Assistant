from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.competitor_product import CompetitorProduct
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.user import User
from app.models.recommendation import Recommendation
from app.services.competitor_service import get_competitor_freshness
from app.services.pricing_workflow import freshness_node, route_freshness, scout_node, strategist_node, compliance_node, persistence_node
from app.services.product_service import create_retailer_product
from app.routes.workflow import ChatRequest, _analysis_response, chat


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def make_product(db, created_days_ago=0):
    user = User(email=f"evidence-{datetime.now().timestamp()}@example.com", retailer_name="Test", password_hash="not-a-secret")
    db.add(user)
    db.flush()
    catalog = ProductCatalog(name="Hibiscus Hair Oil", category="Hair care", brand="Store", product_details="100 ml", created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago))
    db.add(catalog)
    db.flush()
    retailer = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id, cost_price=100, stock_quantity=5, minimum_profit_margin=20)
    db.add(retailer)
    db.flush()
    return catalog, retailer


def add_competitor(db, catalog_id, platform, price, days_old):
    row = CompetitorProduct(catalog_product_id=catalog_id, platform_name=platform, product_name=f"{platform} hair oil", price=price, availability=True, scraped_at=datetime.now(timezone.utc) - timedelta(days=days_old))
    db.add(row)
    db.flush()
    return row


def test_freshness_uses_latest_competitor_observation_not_catalog_age(db):
    catalog, _ = make_product(db, created_days_ago=90)
    add_competitor(db, catalog.id, "Amazon", 250, 12)
    add_competitor(db, catalog.id, "Flipkart", 230, 1)
    result = get_competitor_freshness(db, catalog.id)
    assert result["status"] == "FRESH"
    assert result["age_days"] < 2


def test_fresh_competitor_data_routes_around_scraping(db):
    catalog, _ = make_product(db, created_days_ago=90)
    add_competitor(db, catalog.id, "Amazon", 250, 1)
    state = freshness_node({"db": db, "catalog_id": catalog.id, "workflow": []})
    assert state["fresh"] is True
    assert route_freshness(state) == "rag"
    assert state["evidence_type"] == "LIVE"


def test_stale_data_with_partial_live_success_uses_only_new_successful_rows(db):
    catalog, _ = make_product(db)
    historical = add_competitor(db, catalog.id, "Myntra", 270, 30)

    def scrape(_db, product):
        product.marketplace_statuses = {"Amazon": "BLOCKED", "Flipkart": "OK", "Myntra": "TIMEOUT", "Meesho": "NETWORK_ERROR"}
        _db.add(CompetitorProduct(catalog_product_id=catalog.id, platform_name="Flipkart", product_name="New matching listing", price=240, availability=True, scraped_at=datetime.now(timezone.utc)))
        _db.commit()

    with patch("app.services.scraping_service.scrape_product", side_effect=scrape):
        result = scout_node({"db": db, "catalog_id": catalog.id, "workflow": []})
    assert result["evidence_type"] == "LIVE"
    assert [item.platform_name for item in result["competitors"]] == ["Flipkart"]
    assert historical not in result["competitors"]
    assert result["marketplace_statuses"]["Myntra"] == "TIMEOUT"


def test_stale_data_and_no_live_results_falls_back_to_history(db):
    catalog, retailer = make_product(db)
    old_amazon = add_competitor(db, catalog.id, "Amazon", 250, 30)
    old_flipkart = add_competitor(db, catalog.id, "Flipkart", 230, 31)

    def scrape(_db, product):
        product.marketplace_statuses = {"Amazon": "BLOCKED", "Flipkart": "NETWORK_ERROR", "Myntra": "TIMEOUT", "Meesho": "BLOCKED"}
        _db.commit()

    with patch("app.services.scraping_service.scrape_product", side_effect=scrape):
        result = scout_node({"db": db, "catalog_id": catalog.id, "workflow": []})
    assert result["evidence_type"] == "STALE_FALLBACK"
    assert set(result["competitors"]) == {old_amazon, old_flipkart}
    with patch("app.services.llm_service.generate_structured", return_value={"success": True, "data": {"recommended_price": 250, "confidence": 0.9, "warnings": []}}):
        strategy = strategist_node({"db": db, "retailer_product_id": retailer.id, **result})
    assert strategy["evidence_type"] == "STALE_FALLBACK"
    assert strategy["strategy"]["confidence"] <= 0.55
    assert "Live marketplace prices were unavailable" in strategy["strategy"]["reasoning_summary"]


def test_new_product_with_live_competitor_data_is_live_evidence(db):
    catalog, _ = make_product(db)

    def scrape(_db, product):
        product.marketplace_statuses = {"Amazon": "OK", "Flipkart": "BLOCKED", "Myntra": "EMPTY", "Meesho": "TIMEOUT"}
        _db.add(CompetitorProduct(catalog_product_id=catalog.id, platform_name="Amazon", product_name="Verified similar oil", price=240, availability=True, scraped_at=datetime.now(timezone.utc)))
        _db.commit()

    with patch("app.services.scraping_service.scrape_product", side_effect=scrape):
        result = scout_node({"db": db, "catalog_id": catalog.id, "workflow": []})
    assert result["evidence_type"] == "LIVE"
    assert len(result["competitors"]) == 1
    assert result["competitors"][0].price == 240


def test_stale_history_from_one_marketplace_is_not_enough_to_recommend(db):
    catalog, retailer = make_product(db)
    add_competitor(db, catalog.id, "Amazon", 250, 30)
    def scrape(_db, product):
        product.marketplace_statuses = {"Amazon": "BLOCKED", "Flipkart": "TIMEOUT", "Myntra": "NETWORK_ERROR", "Meesho": "EMPTY"}
        _db.commit()
    with patch("app.services.scraping_service.scrape_product", side_effect=scrape):
        result = scout_node({"db": db, "catalog_id": catalog.id, "workflow": []})
    assert result["evidence_type"] == "STALE_FALLBACK"
    fallback_estimate = {"success": True, "data": {
        "estimated_price": 145, "price_range_low": 130, "price_range_high": 180,
        "confidence": 0.4, "reasoning": "Uses product cost and related pricing context.", "evidence_type": "LLM_ESTIMATE",
    }}
    with patch("app.services.llm_service.generate_structured", return_value=fallback_estimate) as estimate:
        strategy = strategist_node({"db": db, "retailer_product_id": retailer.id, **result})
    estimate.assert_called_once()
    from app.config import OLLAMA_STRATEGIST_TIMEOUT
    assert estimate.call_args.kwargs["timeout"] == OLLAMA_STRATEGIST_TIMEOUT
    assert estimate.call_args.kwargs["operation"] == "strategist_estimate"
    assert strategy["evidence_type"] == "LLM_ESTIMATE"
    assert strategy["strategy"]["recommended_price"] >= 120


def test_product_write_stages_catalog_and_langgraph_selects_final_identity(db):
    catalog, retailer = make_product(db)
    initial_count = db.query(ProductCatalog).count()
    created = create_retailer_product(db, retailer.user_id, "Neem Hair Oil", "Hair care", "Brand B", "Neem oil", 80, 3, 20)
    assert db.query(ProductCatalog).count() == initial_count + 1
    assert created.catalog_product_id != catalog.id  # staging FK; graph owns the final decision


def test_estimate_is_margin_safe_persisted_as_recommendation_only(db):
    catalog, retailer = make_product(db)
    retailer.cost_price = 100.01
    db.flush()
    estimate_response = {"success": True, "data": {
        "estimated_price": 105, "price_range_low": 95, "price_range_high": 125,
        "confidence": 0.55, "reasoning": "Based on the product category and cost basis.", "evidence_type": "LLM_ESTIMATE",
    }}
    with patch("app.services.llm_service.generate_structured", return_value=estimate_response):
        result = strategist_node({"db": db, "retailer_product_id": retailer.id, "competitors": [], "evidence_type": "NO_EVIDENCE"})
    strategy = result["strategy"]
    assert result["evidence_type"] == "LLM_ESTIMATE"
    assert strategy["recommended_price"] >= 120.012
    assert strategy["price_range_low"] >= 120.012
    assert strategy["price_range_low"] <= strategy["recommended_price"] <= strategy["price_range_high"]
    assert strategy["confidence"] <= 0.60
    checked = compliance_node({"db": db, "retailer_product_id": retailer.id, "competitors": [], "strategy": strategy, "evidence_type": "LLM_ESTIMATE"})
    assert checked["compliance"]["accepted"] is True
    persisted = persistence_node({"db": db, "retailer_product_id": retailer.id, "competitors": [], "strategy": strategy, "evidence_type": "LLM_ESTIMATE", **checked})
    assert persisted["recommendation"].evidence_type == "LLM_ESTIMATE"
    assert persisted["recommendation"].recommended_price_min == strategy["price_range_low"]
    assert persisted["recommendation"].recommended_price_max == strategy["price_range_high"]
    assert db.query(CompetitorProduct).filter_by(catalog_product_id=catalog.id).count() == 0
    assert db.query(Recommendation).count() == 1


def test_chat_uses_persisted_recommendation_when_llm_and_rag_are_unavailable(db):
    _, retailer = make_product(db)
    recommendation = Recommendation(
        retailer_product_id=retailer.id, recommended_price=129,
        recommended_price_min=115, recommended_price_max=145,
        expected_profit=29, profit_percentage=29, reasoning="Price follows supplied product and cost evidence.",
        evidence_type="LLM_ESTIMATE", accepted_by_user=False,
    )
    db.add(recommendation); db.commit()
    user = db.query(User).filter_by(id=retailer.user_id).first()
    with patch("app.routes.workflow.retrieve_chatbot_context", return_value=""), \
         patch("app.routes.workflow.retrieve_business_knowledge", return_value=""), \
         patch("app.routes.workflow.generate_structured", return_value={"success": False, "data": None}):
        result = chat(ChatRequest(retailer_product_id=retailer.id, question="About Hibiscus Hair Oil"), db, user)
    assert "₹129" in result["answer"]
    assert "₹115–₹145" in result["answer"]
    assert "grounded answer" not in result["answer"].lower()
    assert result["grounded"] is True


def test_pricing_rag_without_current_competitors_can_retrieve_related_prices():
    from app.services.ai.chroma_service import retrieve_pricing_context
    hits = [{"id": "comp_1", "text": "Competitor product: Similar herbal oil\nPrice: Rs.120", "metadata": {"type": "competitor_price"}, "distance": 0.2}]
    with patch("app.services.ai.chroma_service.retrieve_relevant", return_value=hits) as retrieve:
        context = retrieve_pricing_context("Herbal hair oil botanical product", catalog_product_id=7,
                                           competitor_product_ids=[], retailer_product_id=9, user_id=3)
    assert "Rs.120" in context
    assert retrieve.call_args.kwargs["where"] is None


def test_pricing_rag_does_not_treat_its_own_old_recommendation_as_evidence():
    from app.services.ai.chroma_service import retrieve_pricing_context
    old_recommendation = [{"id": "rec_3_9", "text": "Suggested price: Rs.158.1", "metadata": {
        "type": "pricing_recommendation", "retailer_product_id": "9", "user_id": "3"}}]
    with patch("app.services.ai.chroma_service.retrieve_relevant", return_value=old_recommendation):
        context = retrieve_pricing_context("Gundhal hair oil price", catalog_product_id=70,
                                           retailer_product_id=9, user_id=3, competitor_product_ids=[])
    assert context == ""


def test_chatbot_rag_falls_back_to_related_shared_pricing_context():
    from app.services.ai.chroma_service import retrieve_chatbot_context
    related = [{"id": "comp_4", "text": "Comparable herbal powder price: Rs.135", "metadata": {"type": "competitor_price"}, "distance": 0.2}]
    with patch("app.services.ai.chroma_service.retrieve_relevant", side_effect=[[], related, []]) as retrieve:
        context = retrieve_chatbot_context("price for herbal powder", catalog_product_id=90,
                                           retailer_product_id=12, user_id=5)
    assert "Rs.135" in context
    assert retrieve.call_count == 3


def test_estimate_reasoning_does_not_claim_missing_market_evidence(db):
    from app.routes.workflow import _business_reasoning
    _, retailer = make_product(db)
    recommendation = Recommendation(
        retailer_product_id=retailer.id, recommended_price=129,
        recommended_price_min=125, recommended_price_max=135,
        expected_profit=29, profit_percentage=29, reasoning="Estimate.",
        evidence_type="LLM_ESTIMATE", accepted_by_user=False,
    )
    points = _business_reasoning(db, retailer, recommendation, [])
    all_points = " ".join(points).casefold()
    assert "competitor" not in all_points
    assert "rating" not in all_points
    assert "trend" not in all_points
    assert "minimum profitable price" in all_points


def test_saved_quantity_aware_recommendation_is_returned_by_workflow_api(db):
    catalog, retailer = make_product(db)
    retailer.quantity_value = 50
    retailer.quantity_unit = "g"
    recommendation = Recommendation(
        retailer_product_id=retailer.id, recommended_price=154,
        recommended_price_min=154, recommended_price_max=161,
        expected_profit=54, profit_percentage=54,
        reasoning="The pack size was compared with compatible listings and the margin floor was respected.",
        evidence_type="LIVE", accepted_by_user=False,
    )
    competitor = CompetitorProduct(catalog_product_id=catalog.id, platform_name="Amazon",
        product_name="Comparable 100 g hair oil", price=300, quantity_value=100,
        quantity_unit="g", pack_count=1, total_quantity=100, total_quantity_unit="g",
        availability=True)
    db.add_all([recommendation, competitor]); db.commit(); db.refresh(recommendation)
    payload = _analysis_response(db, retailer, {
        "recommendation": recommendation, "competitors": [competitor], "evidence_type": "LIVE",
        "compliance": {"accepted": True, "reasons": []}, "strategy": {},
    })["recommendation"]
    assert payload["id"] == recommendation.id
    assert payload["recommended_price"] == 154
    assert payload["recommended_price_min"] == 154
    assert payload["recommended_price_max"] == 161
    assert payload["basis"] == "MARKET_DATA"
    assert any("Comparable listings" in point and "₹150" in point for point in payload["reasoning"])


def test_unavailable_estimator_returns_cost_based_starting_price(db):
    _, retailer = make_product(db)
    with patch("app.services.llm_service.generate_structured", return_value={"success": False, "data": None}):
        result = strategist_node({"db": db, "retailer_product_id": retailer.id, "competitors": [], "evidence_type": "NO_EVIDENCE"})
    assert result["evidence_type"] == "LLM_ESTIMATE"
    assert result["strategy"]["recommended_price"] > 0
    assert result["strategy"]["price_range_low"] <= result["strategy"]["recommended_price"] <= result["strategy"]["price_range_high"]
    assert "provisional" in result["strategy"]["reasoning_summary"]
    persisted = compliance_node({"db": db, "retailer_product_id": retailer.id, "competitors": [], "strategy": result["strategy"], "evidence_type": result["evidence_type"]})
    assert persisted["compliance"]["accepted"]
    result.update({"db": db, "retailer_product_id": retailer.id, **persisted})
    saved = persistence_node(result)
    assert saved["recommendation"].recommended_price > 0
    assert db.query(Recommendation).count() == 1
