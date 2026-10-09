from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.recommendation import Recommendation
from app.models.user import User
from app.services.pricing_workflow import compliance_node, persistence_node, route_identity, route_freshness, pricing_graph
from scraping.scout import _failure_status


def test_graph_has_bounded_four_agent_flow_nodes():
    nodes = set(pricing_graph.get_graph().nodes)
    assert {"identity", "catalog_decision", "freshness", "scout", "rag", "strategist", "compliance", "persistence"}.issubset(nodes)
    assert route_identity({"error": "uncertain"}) == "stop"
    assert route_identity({"identity": {"decision": "MATCH"}}) == "catalog"
    assert route_freshness({"fresh": True}) == "rag"
    assert route_freshness({"fresh": False}) == "scout"


def test_compliance_rejects_missing_market_evidence():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(email="workflow@example.com", retailer_name="Test", password_hash="not-a-secret")
    session.add(user); session.flush()
    catalog = ProductCatalog(name="Test product", category="Care")
    session.add(catalog); session.flush()
    product = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id, cost_price=100, stock_quantity=5, minimum_profit_margin=20)
    session.add(product); session.flush()
    result = compliance_node({"db": session, "retailer_product_id": product.id, "strategy": {"recommended_price": 130}})
    assert result["compliance"]["accepted"] is False
    assert "No competitor evidence" in result["compliance"]["reasons"][0]
    assert session.query(Recommendation).count() == 0
    session.close(); engine.dispose()


def test_compliance_persists_only_margin_safe_evidence_based_price():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user = User(email="compliance@example.com", retailer_name="Test", password_hash="not-a-secret")
    session.add(user); session.flush()
    catalog = ProductCatalog(name="Test product", category="Care")
    session.add(catalog); session.flush()
    product = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id, cost_price=100, stock_quantity=5, minimum_profit_margin=20)
    session.add(product); session.flush()
    state = {"db": session, "retailer_product_id": product.id, "competitors": [object()], "evidence_type": "LIVE", "strategy": {"recommended_price": 125, "confidence": 0.8, "reasoning_summary": "Evidence-based."}}
    result = compliance_node(state)
    assert result["compliance"]["accepted"] is True
    assert session.query(Recommendation).count() == 0
    state.update(result)
    persisted = persistence_node(state)
    assert persisted["recommendation"].recommended_price == 125
    assert session.query(Recommendation).count() == 1
    session.close(); engine.dispose()


def test_scraper_failure_classification_distinguishes_challenges_and_network_errors():
    assert _failure_status("FAILED", "CAPTCHA verification") == "BLOCKED"
    assert _failure_status("FAILED", "connection reset") == "NETWORK_ERROR"
    assert _failure_status("FAILED", "navigation timed out") == "TIMEOUT"
