import json
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
from app.services.pricing_workflow import rag_node, strategist_node


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _product_and_competitor(db):
    user = User(email="strategist-context@example.test", retailer_name="Test", password_hash="x")
    db.add(user)
    db.flush()
    catalog = ProductCatalog(name="Herbal Hair Oil", category="Hair care", brand="Example",
                             product_details="Herbal hair oil, 100 ml")
    db.add(catalog)
    db.flush()
    product = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id,
                              name_override="Herbal Hair Oil", category_override="Hair care",
                              brand_override="Example", product_details_override="Herbal hair oil, 100 ml",
                              cost_price=100, stock_quantity=5, minimum_profit_margin=20,
                              quantity_value=100, quantity_unit="ml")
    db.add(product)
    db.flush()
    competitor = CompetitorProduct(
        catalog_product_id=catalog.id, platform_name="Myntra", product_name="Herbal Hair Oil 200 ml",
        product_url="https://market.example/item/1", product_details="DETAILS_MUST_NOT_BE_COPIED_FROM_RAG",
        price=300, quantity_value=200, quantity_unit="ml", pack_count=1,
        rating=4.6, availability=True,
    )
    db.add(competitor)
    db.commit()
    return product, catalog, competitor


def _prompt_context(prompt):
    return json.loads(prompt.split("\n", 1)[1])


def test_competitor_pricing_omits_rag_but_keeps_structured_evidence(db):
    product, _, competitor = _product_and_competitor(db)
    rag_text = "DUPLICATED_COMPETITOR_RAG_SENTINEL " + ("long scraped detail " * 2000)
    response = {"success": True, "data": {
        "suggested_price": 180, "price_range_low": 160, "price_range_high": 210,
        "rationale": "Local estimate informed by the supplied product information.",
        "assumptions": "The pack is sold as one bottle.",
    }}
    with patch("app.services.llm_service.generate_structured", return_value=response) as generate:
        result = strategist_node({
            "db": db, "retailer_product_id": product.id, "competitors": [competitor],
            "evidence_type": "LIVE", "rag_context": rag_text,
            "marketplace_statuses": {"Myntra": "SUCCESS"},
        })

    prompt = generate.call_args.kwargs["prompt"]
    from app.config import OLLAMA_MODEL
    assert generate.call_args.kwargs["model"] == OLLAMA_MODEL
    assert generate.call_args.kwargs["operation"] == "pricing_fallback"
    context = _prompt_context(prompt)
    assert "DUPLICATED_COMPETITOR_RAG_SENTINEL" not in prompt
    assert "rag_context" not in context
    assert len(context["validated_market_evidence"]["offers"]) == 1
    evidence = context["validated_market_evidence"]["offers"][0]
    assert evidence["marketplace"] == "Myntra"
    assert evidence["product_name"] == competitor.product_name
    assert evidence["product_url"] == competitor.product_url
    assert evidence["listed_price"] == 300
    assert evidence["price"] == 150
    assert evidence["pack_quantity"] == 200
    assert evidence["pack_unit"] == "ml"
    assert evidence["pack_count"] == 1
    assert result["strategy"]["recommended_price"] == 180
    assert result["strategy"]["pricing_method"] == "market_data_plus_local_ai"


def test_no_competitors_and_unavailable_rag_use_safe_estimate_fallback(db):
    product, _, _ = _product_and_competitor(db)
    observed = {}

    def fail_estimate(prompt, **kwargs):
        observed["prompt"] = prompt
        return {"success": False, "data": None, "error": "unavailable"}

    with patch("app.services.llm_service.generate_structured", side_effect=fail_estimate):
        result = strategist_node({
            "db": db, "retailer_product_id": product.id, "competitors": [],
            "evidence_type": "NO_EVIDENCE", "rag_context": None,
        })

    context = _prompt_context(observed["prompt"])
    assert context["relevant_pricing_context"] == "No RAG market context is asserted; only validated marketplace observations above are market evidence."
    assert result["strategy"]["recommended_price"] is None
    assert result["strategy"]["minimum_viable_price"] == 120
    assert result["strategy"]["evidence_type"] == "ESTIMATION_UNAVAILABLE"
    assert result["strategy"]["estimation_status"] == "unavailable"
    assert result["workflow"][-1] == {"step": "STRATEGIST", "status": "COMPLETED"}


def test_contradictory_local_estimate_is_not_presented_as_a_recommendation(db, monkeypatch):
    product, _, _ = _product_and_competitor(db)
    monkeypatch.setattr("app.services.llm_service.generate_structured", lambda **kwargs: {
        "success": True, "data": {"suggested_price": 180, "price_range_low": 220,
                                   "price_range_high": 210, "rationale": "contradictory"},
    })
    result = strategist_node({
        "db": db, "retailer_product_id": product.id, "competitors": [],
        "evidence_type": "NO_EVIDENCE", "marketplace_statuses": {"Amazon": "BLOCKED"},
    })
    assert result["strategy"]["recommended_price"] is None
    assert result["strategy"]["estimation_status"] == "unavailable"
    assert result["strategy"]["minimum_viable_price"] == 120


def test_rag_node_still_retrieves_and_returns_context_for_other_consumers(db):
    product, catalog, competitor = _product_and_competitor(db)
    retained_context = "RETAINED_RAG_FOR_ESTIMATE_OR_OTHER_CONSUMER"
    with patch("app.services.ai.chroma_service.index_product_catalog"), \
         patch("app.services.ai.chroma_service.index_competitor_price"), \
         patch("app.services.ai.chroma_service.index_retailer_product"), \
         patch("app.services.ai.chroma_service.retrieve_pricing_context", return_value=retained_context) as retrieve:
        result = rag_node({
            "db": db, "retailer_product_id": product.id, "catalog_id": catalog.id,
            "competitors": [competitor], "workflow": [],
        })

    assert result["rag_context"] == retained_context
    assert retrieve.call_args.kwargs["competitor_product_ids"] == [competitor.id]
