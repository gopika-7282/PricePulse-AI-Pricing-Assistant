from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.product_catalog import ProductCatalog
from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory
from app.models.recommendation import Recommendation
from app.models.retailer_product import RetailerProduct
from app.models.user import User
from app.services.product_service import (
    delete_retailer_product, get_retailer_product_by_id, get_retailer_products,
    update_retailer_product,
)
from app.services.recommendation_service import get_all_recommendations_for_user


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def setup_shared_product(db):
    user_a = User(email="a@example.test", retailer_name="A", password_hash="x")
    user_b = User(email="b@example.test", retailer_name="B", password_hash="x")
    db.add_all([user_a, user_b]); db.flush()
    catalog = ProductCatalog(name="Hibiscus Hair Oil", category="Hair care", brand="Shared", product_details="100 ml")
    db.add(catalog); db.flush()
    a = RetailerProduct(user_id=user_a.id, catalog_product_id=catalog.id, cost_price=100, stock_quantity=5, minimum_profit_margin=20)
    b = RetailerProduct(user_id=user_b.id, catalog_product_id=catalog.id, cost_price=120, stock_quantity=8, minimum_profit_margin=25)
    db.add_all([a, b]); db.commit()
    return user_a, user_b, catalog, a, b


def test_product_list_and_lookup_are_scoped_to_authenticated_owner(db):
    a_user, b_user, _, a, b = setup_shared_product(db)
    assert get_retailer_products(db, a_user.id) == [a]
    assert get_retailer_product_by_id(db, b.id, a_user.id) is None
    assert get_retailer_product_by_id(db, a.id, a_user.id) == a


def test_product_routes_derive_owner_from_authenticated_user(db, monkeypatch):
    from fastapi import HTTPException
    from app.routes.product import get_products, get_product, update_product, delete_product
    from app.schemas.retailer_product import ProductUpdateRequest

    a_user, b_user, catalog, a, b = setup_shared_product(db)
    from app.services.ai import chroma_service
    monkeypatch.setattr(chroma_service, "delete_retailer_product_context", lambda *args, **kwargs: None)
    assert get_products(db, a_user) == [a]
    with pytest.raises(HTTPException) as hidden:
        get_product(b.id, db, a_user)
    assert hidden.value.status_code == 404

    request = ProductUpdateRequest(product_name=catalog.name, category=catalog.category, brand=catalog.brand,
        product_details=catalog.product_details, cost_price=155, stock_quantity=4, minimum_profit_margin=30)
    updated = update_product(a.id, request, db, a_user)
    assert updated.cost_price == 155
    with pytest.raises(HTTPException) as unauthorized_edit:
        update_product(b.id, request, db, a_user)
    assert unauthorized_edit.value.status_code == 404
    with pytest.raises(HTTPException) as unauthorized_delete:
        delete_product(b.id, db, a_user)
    assert unauthorized_delete.value.status_code == 404
    assert delete_product(a.id, db, a_user) is None
    assert db.query(ProductCatalog).filter_by(id=catalog.id).one().name == catalog.name


def test_owner_can_edit_but_other_user_cannot(db):
    a_user, b_user, catalog, a, b = setup_shared_product(db)
    updated = update_retailer_product(db, a.id, a_user.id, "Hibiscus Hair Oil", "Hair care", "Shared", "100 ml", 150, 3, 30)
    assert updated.cost_price == 150
    assert updated.catalog_identity_pending is True
    assert update_retailer_product(db, b.id, a_user.id, "Other", "Other", "Other", "Other", 1, 1, 1) is None
    assert b.cost_price == 120


def test_identity_edit_is_saved_for_langgraph_reevaluation(db):
    a_user, _, catalog, a, _ = setup_shared_product(db)
    updated = update_retailer_product(db, a.id, a_user.id, "Hibiscus Hair Serum", "Hair care", "Shared", "100 ml", 140, 4, 22)
    db.expire_all()
    assert updated.product_name == "Hibiscus Hair Serum"
    assert a.catalog_product_id == catalog.id
    assert a.cost_price == 140


def test_owner_can_delete_without_deleting_shared_catalog_or_other_retailer_entry(db):
    a_user, b_user, catalog, a, b = setup_shared_product(db)
    assert delete_retailer_product(db, b.id, a_user.id) is False
    assert delete_retailer_product(db, a.id, a_user.id) is True
    db.expire_all()
    assert get_retailer_product_by_id(db, b.id, b_user.id) is not None
    preserved = db.query(ProductCatalog).filter_by(id=catalog.id).one()
    assert preserved.name == "Hibiscus Hair Oil"
    # A subsequent retailer can still attach a retailer-owned row to the same
    # preserved shared catalog identity.
    later = RetailerProduct(user_id=b_user.id, catalog_product_id=preserved.id, cost_price=90, stock_quantity=2, minimum_profit_margin=10)
    db.add(later); db.commit()
    assert later.catalog_product_id == catalog.id


def test_delete_cleans_only_an_unresolved_staging_catalog(db):
    a_user, b_user, shared, a, b = setup_shared_product(db)
    staging = ProductCatalog(name="Staging product", category="Other", product_details="Pending")
    db.add(staging); db.flush()
    a.catalog_product_id = staging.id
    a.catalog_identity_pending = True
    a.catalog_identity_staging = True
    db.commit()
    assert delete_retailer_product(db, a.id, a_user.id) is True
    assert db.query(ProductCatalog).filter_by(id=staging.id).first() is None
    assert db.query(ProductCatalog).filter_by(id=shared.id).one().name == "Hibiscus Hair Oil"
    assert get_retailer_product_by_id(db, b.id, b_user.id) is not None


def test_chatbot_context_keeps_shared_catalog_but_filters_other_users_recommendations(monkeypatch):
    from app.services.ai import chroma_service

    hits = [
        {"text": "Shared product description", "metadata": {"type": "catalog_product", "catalog_product_id": "4"}},
        {"text": "My private product details", "metadata": {"type": "retailer_product", "catalog_product_id": "4", "retailer_product_id": "20", "user_id": "2"}},
        {"text": "Another retailer private details", "metadata": {"type": "retailer_product", "catalog_product_id": "4", "retailer_product_id": "21", "user_id": "3"}},
        {"text": "My recommendation", "metadata": {"type": "pricing_recommendation", "catalog_product_id": "4", "retailer_product_id": "20", "user_id": "2"}},
        {"text": "Private recommendation", "metadata": {"type": "pricing_recommendation", "catalog_product_id": "4", "retailer_product_id": "21", "user_id": "3"}},
    ]
    monkeypatch.setattr(chroma_service, "retrieve_relevant", lambda **kwargs: hits)
    context = chroma_service.retrieve_chatbot_context("tell me about product", 4, retailer_product_id=20, user_id=2)
    assert "Shared product description" in context
    assert "My private product details" in context
    assert "Another retailer private details" not in context
    assert "My recommendation" in context
    assert "Private recommendation" not in context


def test_business_reasoning_uses_prices_ratings_and_only_sufficient_recent_trends(db):
    from app.routes.workflow import _business_reasoning

    a_user, _, catalog, a, _ = setup_shared_product(db)
    recent = datetime.now(timezone.utc)
    first = CompetitorProduct(catalog_product_id=catalog.id, platform_name="Amazon", product_name="Oil A", price=160, rating=4.2, scraped_at=recent)
    second = CompetitorProduct(catalog_product_id=catalog.id, platform_name="Flipkart", product_name="Oil B", price=185, rating=None, scraped_at=recent)
    db.add_all([first, second]); db.flush()
    db.add_all([
        CompetitorPriceHistory(competitor_product_id=first.id, price=150, scraped_at=recent.replace(day=max(1, recent.day - 1))),
        CompetitorPriceHistory(competitor_product_id=first.id, price=160, scraped_at=recent),
        CompetitorPriceHistory(competitor_product_id=second.id, price=175, scraped_at=recent.replace(day=max(1, recent.day - 1))),
        CompetitorPriceHistory(competitor_product_id=second.id, price=185, scraped_at=recent),
    ]); db.flush()
    rec = Recommendation(retailer_product_id=a.id, recommended_price=170, expected_profit=50, profit_percentage=50, reasoning="internal", evidence_type="LIVE")
    points = _business_reasoning(db, a, rec, [first, second])
    assert len(points) <= 4
    assert any("₹160.00–₹185.00" in point for point in points)
    assert any("stars" in point and "4.2" in point for point in points)
    assert any("moved up" in point for point in points)
    assert all("LIVE" not in point and "evidence_type" not in point for point in points)


def test_recommendation_list_returns_only_owner_with_grounded_business_reasoning(db):
    from app.routes.recommendation import get_recommendations

    a_user, _, catalog, a, b = setup_shared_product(db)
    rec_a = Recommendation(retailer_product_id=a.id, recommended_price=170, expected_profit=70, profit_percentage=70, reasoning="stored summary", evidence_type="LIVE")
    rec_b = Recommendation(retailer_product_id=b.id, recommended_price=190, expected_profit=70, profit_percentage=58, reasoning="private summary", evidence_type="LIVE")
    comp = CompetitorProduct(catalog_product_id=catalog.id, platform_name="Amazon", product_name="Comparable oil", price=160, rating=4.1)
    db.add_all([rec_a, rec_b, comp]); db.commit()
    results = get_recommendations(db, a_user)
    assert [item["retailer_product_id"] for item in results] == [a.id]
    assert len(results[0]["reasoning_points"]) <= 4
    assert any("₹160.00–₹160.00" in point for point in results[0]["reasoning_points"])


def test_marketplace_business_display_uses_safe_missing_value_labels_and_valid_equivalents(db):
    from app.routes.workflow import _analysis_response

    _, _, catalog, product, _ = setup_shared_product(db)
    product.quantity_value = 100
    product.quantity_unit = "ml"
    unknown = CompetitorProduct(catalog_product_id=catalog.id, platform_name="Meesho",
        product_name="Oil with unknown size", price=100, rating=None, availability=None)
    known = CompetitorProduct(catalog_product_id=catalog.id, platform_name="Amazon",
        product_name="Oil 200 ml", price=250, quantity_value=200, quantity_unit="ml",
        rating=4.2, availability=True)
    db.add_all([unknown, known])
    db.flush()

    payload = _analysis_response(db, product, {"competitors": [unknown, known]})
    meesho = payload["marketplace_products"]["Meesho"][0]
    amazon = payload["marketplace_products"]["Amazon"][0]

    assert meesho["rating_display"] == "Not rated"
    assert unknown.availability is None
    assert meesho["quantity_display"] == "Not specified"
    assert meesho["equivalent_price_display"] == "Not available"
    assert meesho["availability_display"] == "Not reported"
    assert meesho["product_details_display"] == "Not available"
    assert amazon["equivalent_price"] == 125
    assert amazon["equivalent_price_display"] == "₹125.00 per 100 ml"


def test_recommendation_list_uses_latest_saved_price_per_product(db):
    user, _, _, product, _ = setup_shared_product(db)
    old = Recommendation(retailer_product_id=product.id, recommended_price=170, expected_profit=70, profit_percentage=70, reasoning="old", evidence_type="ESTIMATE")
    latest = Recommendation(retailer_product_id=product.id, recommended_price=180, expected_profit=80, profit_percentage=80, reasoning="latest", evidence_type="ESTIMATE")
    db.add_all([old, latest]); db.commit()
    results = get_all_recommendations_for_user(db, user.id)
    assert len(results) == 1
    assert results[0].recommended_price == 180


def test_accept_price_persists_once_and_is_idempotent_for_the_owner(db):
    from app.routes.recommendation import accept_recommendation

    a_user, b_user, _, a, b = setup_shared_product(db)
    own = Recommendation(retailer_product_id=a.id, recommended_price=175, expected_profit=75, profit_percentage=75, reasoning="Market context", evidence_type="LIVE", accepted_by_user=False)
    other = Recommendation(retailer_product_id=b.id, recommended_price=190, expected_profit=70, profit_percentage=58, reasoning="Private", evidence_type="LIVE", accepted_by_user=False)
    db.add_all([own, other]); db.commit(); db.refresh(own)
    result = accept_recommendation(own.id, db, a_user)
    assert result["accepted"] is True
    assert result["accepted_price"] == 175
    assert result["task_status"] == "COMPLETED"
    assert own.accepted_by_user is True
    assert accept_recommendation(own.id, db, a_user)["already_accepted"] is True
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as denied:
        accept_recommendation(other.id, db, a_user)
    assert denied.value.status_code == 404
    assert other.accepted_by_user is False


def test_langgraph_stage_events_are_emitted_from_actual_wrapped_nodes():
    from app.services.pricing_workflow import _tracked_node

    events = []
    wrapped = _tracked_node("market_review", "Reviewing available market information", lambda state: {"workflow": [{"step": "CATALOG", "status": "COMPLETED"}]})
    result = wrapped({"progress_callback": events.append})
    assert result["workflow"][0]["status"] == "COMPLETED"
    assert [event["status"] for event in events] == ["running", "completed"]
    assert all("CATALOG" not in event["message"] for event in events)


def test_langgraph_catalog_decision_reuses_match_without_creating_duplicate(db):
    from app.services.pricing_workflow import catalog_decision_node

    user, _, catalog, owned, _ = setup_shared_product(db)
    before = db.query(ProductCatalog).count()
    result = catalog_decision_node({
        "db": db, "retailer_product_id": owned.id,
        "identity": {"decision": "MATCH", "matched_catalog_id": catalog.id}, "workflow": [],
    })
    assert result["catalog_id"] == catalog.id
    assert owned.catalog_product_id == catalog.id
    assert db.query(ProductCatalog).count() == before


def test_initial_uncertain_decision_replaces_staging_catalog_in_graph(db):
    from app.services.pricing_workflow import catalog_decision_node

    user, other_user, shared, owned, other = setup_shared_product(db)
    staging = ProductCatalog(name=owned.product_name, category=owned.category, brand=owned.brand,
                             product_details=owned.product_details)
    db.add(staging); db.flush()
    old_staging_id = staging.id
    owned.catalog_product_id = staging.id
    owned.catalog_identity_pending = True
    owned.catalog_identity_staging = True
    db.commit()
    result = catalog_decision_node({
        "db": db, "retailer_product_id": owned.id,
        "identity": {"decision": "UNCERTAIN_MATCH", "matched_catalog_id": None}, "workflow": [],
    })
    assert result["catalog_id"] != old_staging_id
    assert owned.catalog_identity_pending is False
    assert db.query(ProductCatalog).filter_by(id=old_staging_id).first() is None
    assert db.query(ProductCatalog).filter_by(id=shared.id).one().name == "Hibiscus Hair Oil"
    assert get_retailer_product_by_id(db, other.id, other_user.id) is not None


@pytest.mark.parametrize("decision", ["NOT_MATCH", "UNCERTAIN_MATCH"])
def test_langgraph_catalog_decision_creates_catalog_for_nonmatch_and_uncertain(db, decision):
    from app.services.pricing_workflow import catalog_decision_node

    user_a, user_b, catalog, owned, other = setup_shared_product(db)
    owned.name_override = "Different Herbal Shampoo"
    owned.product_details_override = "A shampoo with a different intended use"
    db.commit()
    result = catalog_decision_node({
        "db": db, "retailer_product_id": owned.id,
        "identity": {"decision": decision, "matched_catalog_id": None}, "workflow": [],
    })
    assert result["catalog_id"] != catalog.id
    assert owned.catalog_product_id == result["catalog_id"]
    assert db.query(ProductCatalog).filter_by(id=catalog.id).one().name == "Hibiscus Hair Oil"
    assert get_retailer_product_by_id(db, other.id, user_b.id) is not None


def test_identity_service_unavailable_is_an_error_not_an_identity_decision(db, monkeypatch):
    from app.services import pricing_workflow
    from app.services.product_identity_service import IdentityServiceUnavailable

    user, _, _, owned, _ = setup_shared_product(db)
    from app.services import product_identity_service
    monkeypatch.setattr(product_identity_service, "evaluate_product_identity", lambda *args, **kwargs: (_ for _ in ()).throw(IdentityServiceUnavailable("oom")))
    result = pricing_workflow.identity_node({"db": db, "retailer_product_id": owned.id, "workflow": []})
    assert result["identity_error"]
    assert "identity" not in result
    assert result["workflow"][-1]["status"] == "FAILED"


def test_private_product_rag_context_upserts_and_deletes_without_touching_shared_docs(monkeypatch):
    from app.services.ai import chroma_service

    stored, removed = [], []
    monkeypatch.setattr(chroma_service, "store_document", lambda **kwargs: stored.append(kwargs))
    monkeypatch.setattr(chroma_service, "delete_document", lambda collection, doc_id: removed.append((collection, doc_id)))
    for details in ("original details", "updated details"):
        chroma_service.index_retailer_product(12, 3, 7, "Oil", product_details=details)
    assert stored[0]["doc_id"] == stored[1]["doc_id"] == "retailer_3_12"
    assert stored[1]["metadata"]["user_id"] == "3"
    assert "updated details" in stored[1]["text"]
    chroma_service.delete_retailer_product_context(12, 3)
    assert removed == [
        (chroma_service.CHATBOT_RAG_COLLECTION, "retailer_3_12"),
        (chroma_service.PRICING_RAG_COLLECTION, "rec_3_12"),
        (chroma_service.CHATBOT_RAG_COLLECTION, "pricing_rec_3_12"),
        (chroma_service.PRICING_RAG_COLLECTION, "rec_12"),
        (chroma_service.CHATBOT_RAG_COLLECTION, "pricing_rec_12"),
    ]
