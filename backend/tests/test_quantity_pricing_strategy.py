import pytest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.competitor_product import CompetitorProduct
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.user import User
from app.services.pricing_workflow import strategist_node, compliance_node
from app.services.relevance_filter_service import classify_candidate
from app.services.pricing_service import minimum_viable_selling_price
from app.services.pricing_service import calculate_market_average
from app.schemas.retailer_product import ProductCreateRequest
from app.utils.quantity import mark_quantity_uncertain


@pytest.fixture(autouse=True)
def local_estimate_stub(monkeypatch):
    monkeypatch.setattr("app.services.llm_service.generate_structured", lambda **kwargs: {
        "success": True, "data": {"suggested_price": 150, "price_range_low": 130,
                                   "price_range_high": 170, "rationale": "Test estimate.",
                                   "assumptions": "Test assumptions."},
    })


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
                             product_name=f"{platform} Hibiscus Hair Oil", price=price,
                             quantity_value=quantity_value, quantity_unit=quantity_unit,
                             scraped_at=datetime.now(timezone.utc), observation_source="SCRAPE")


def test_product_request_rejects_invalid_pricing_inputs():
    valid = {
        "product_name": "Hibiscus Oil", "category": "Hair Care", "brand": "Garden",
        "product_details": "Oil", "cost_price": 70, "stock_quantity": 0,
        "minimum_profit_margin": 0,
    }
    assert ProductCreateRequest.model_validate(valid).cost_price == 70
    for field, value in (("cost_price", 0), ("cost_price", -1),
                         ("stock_quantity", -1), ("minimum_profit_margin", -1)):
        with pytest.raises(ValueError):
            ProductCreateRequest.model_validate({**valid, field: value})


def test_minimum_price_rejects_zero_cost_and_negative_margin():
    with pytest.raises(ValueError, match="Cost price"):
        minimum_viable_selling_price(0, 20)
    with pytest.raises(ValueError, match="margin"):
        minimum_viable_selling_price(100, -1)


def test_sufficient_market_evidence_uses_market_median_without_local_estimation(monkeypatch):
    monkeypatch.setattr("app.services.llm_service.generate_structured", lambda **kwargs: pytest.fail("LLM should not run for sufficient market evidence"))
    engine, db, catalog, product = _session_with_product()
    competitors = [
        _competitor(catalog, "Amazon", 180, 100, "ml"),
        _competitor(catalog, "Flipkart", 250, 200, "ml"),
        _competitor(catalog, "Myntra", 120, 50, "ml"),
    ]
    db.add_all(competitors)
    db.flush()
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                             "competitors": competitors, "evidence_type": "LIVE"})["strategy"]

    assert result["minimum_viable_price"] == 91
    assert result["market_reference_price"] == 180
    assert result["market_statistics"]["comparable_offer_count"] == 3
    assert result["market_statistics"]["distinct_marketplace_count"] == 3
    assert result["market_statistics"]["mean_price"] == pytest.approx(181.67)
    assert result["recommended_price"] == 180
    assert result["expected_margin"] == pytest.approx(157.14)
    assert result["gross_profit_per_unit"] == 110
    assert result["markup_on_cost_percent"] == pytest.approx(157.14)
    assert result["gross_margin_percent"] == pytest.approx(61.11)
    assert result["price_range_low"] == 152.5
    assert result["price_range_high"] == 210
    assert result["market_statistics"]["observed_minimum_price"] == 125
    assert result["market_statistics"]["observed_maximum_price"] == 240
    assert "median comparable-market reference" in result["reasoning_summary"]
    assert "₹91.00" in result["reasoning_summary"]
    db.close()
    engine.dispose()


def test_sparse_market_reference_is_combined_with_local_estimate_and_remains_provisional():
    engine, db, catalog, product = _session_with_product()
    competitor = _competitor(catalog, "Amazon", 180, 100, "ml")
    db.add(competitor)
    db.flush()
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                              "competitors": [competitor], "evidence_type": "LIVE"})["strategy"]

    assert result["minimum_viable_price"] == 91
    assert result["recommended_price"] == 150
    assert result["market_reference_price"] == 180
    assert result["expected_margin"] == pytest.approx(114.29)
    assert result["margin_headroom"] == pytest.approx(84.29)
    assert result["price_range_low"] == 130
    assert result["price_range_high"] == 170
    assert result["confidence"] == 0.30
    assert result["evidence_type"] == "PROVISIONAL"
    assert result["pricing_method"] == "market_data_plus_local_ai"
    db.close()
    engine.dispose()


def test_nine_offers_from_one_marketplace_remain_low_confidence_and_provisional():
    engine, db, catalog, product = _session_with_product(quantity_value=250, quantity_unit="ml")
    offers = []
    for index in range(9):
        offer = _competitor(catalog, "Amazon", 150 + index * 10, 250, "ml")
        offer.product_name = f"Hibiscus Hair Oil edition {index}"
        offer.product_url = f"https://amazon.example/product/{index}"
        offers.append(offer)
    db.add_all(offers)
    db.flush()
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                              "competitors": offers, "evidence_type": "LIVE"})["strategy"]

    assert result["market_statistics"]["comparable_offer_count"] == 9
    assert result["market_statistics"]["distinct_marketplace_count"] == 1
    assert result["evidence_type"] == "PROVISIONAL"
    assert result["confidence_label"] == "Low"
    assert "Market data + local AI estimate" in result["reasoning_summary"]
    db.close()
    engine.dispose()


def test_near_duplicate_same_marketplace_listing_is_counted_once():
    engine, db, catalog, product = _session_with_product(quantity_value=250, quantity_unit="ml")
    first = _competitor(catalog, "Amazon", 250, 250, "ml")
    first.product_name = "Hibiscus Hair Oil 250 ml"
    first.product_url = "https://amazon.example/product/one"
    second = _competitor(catalog, "Amazon", 251, 250, "ml")
    second.product_name = "Hibiscus Hair Oil 250ml"
    second.product_url = "https://amazon.example/product/two"
    db.add_all([first, second])
    db.flush()

    strategy = strategist_node({"db": db, "retailer_product_id": product.id,
                                "competitors": [first, second], "evidence_type": "LIVE"})["strategy"]
    assert strategy["market_statistics"]["comparable_offer_count"] == 1
    assert strategy["market_statistics"]["exclusion_reasons"] == {"duplicate_offer": 1}
    db.close()
    engine.dispose()


def test_multipack_offer_is_normalized_to_the_same_250_ml_basis():
    engine, db, catalog, product = _session_with_product(quantity_value=250, quantity_unit="ml")
    offer = _competitor(catalog, "Amazon", 400, 500, "ml")
    offer.pack_count = 2
    offer.product_name = "Hibiscus Hair Oil 500 ml pack of 2"
    db.add(offer)
    db.flush()
    strategy = strategist_node({"db": db, "retailer_product_id": product.id,
                                "competitors": [offer], "evidence_type": "LIVE"})["strategy"]
    assert strategy["market_statistics"]["median_price"] == 100
    assert strategy["market_statistics"]["comparable_offer_count"] == 1
    assert strategy["market_statistics"]["exclusion_count"] == 0
    db.close()
    engine.dispose()


def test_compliance_rejects_a_price_below_the_deterministic_floor():
    engine, db, catalog, product = _session_with_product()
    result = compliance_node({
        "db": db, "retailer_product_id": product.id, "competitors": [],
        "evidence_type": "COST_ESTIMATE",
        "strategy": {"recommended_price": 90, "confidence": 0.2},
    })
    assert result["compliance"]["accepted"] is False
    assert "does not meet the minimum profit margin" in result["compliance"]["reasons"][0]
    db.close()
    engine.dispose()


def test_missing_quantity_is_excluded_and_uses_cost_floor():
    engine, db, catalog, product = _session_with_product()
    unknown = _competitor(catalog, "Meesho", 100)
    db.add(unknown)
    db.flush()
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                             "competitors": [unknown], "evidence_type": "LIVE"})["strategy"]
    assert result["market_statistics"]["comparable_offer_count"] == 0
    assert result["market_statistics"]["exclusion_reasons"] == {"quantity_unavailable": 1}
    assert result["recommended_price"] == 150
    assert result["minimum_viable_price"] == 91
    assert result["price_range_low"] == 130 and result["price_range_high"] == 170
    assert result["confidence"] == 0.35
    assert result["evidence_type"] == "LLM_ESTIMATE"
    assert "Local AI estimate — no sufficient live market data" in result["reasoning_summary"]
    db.close()
    engine.dispose()


def test_incompatible_mass_and_volume_do_not_enter_market_comparison():
    engine, db, catalog, product = _session_with_product()
    competitor = _competitor(catalog, "Amazon", 200, 100, "g")
    db.add(competitor)
    db.flush()
    result = strategist_node({"db": db, "retailer_product_id": product.id,
                             "competitors": [competitor], "evidence_type": "LIVE"})

    assert result["evidence_type"] == "LLM_ESTIMATE"
    assert result["strategy"]["recommended_price"] == 150
    assert result["strategy"]["market_statistics"]["exclusion_reasons"] == {"incompatible_quantity_dimension": 1}
    db.close()
    engine.dispose()


def test_ambiguous_offer_is_excluded_from_normalized_market_input():
    engine, db, catalog, product = _session_with_product(quantity_value=100, quantity_unit="g")
    verified = _competitor(catalog, "Amazon", 180, 100, "g")
    ambiguous = _competitor(catalog, "Amazon", 500, 100, "g")
    ambiguous.pack_count = 1
    ambiguous.total_quantity = 100
    ambiguous.total_quantity_unit = "g"
    ambiguous.product_details = mark_quantity_uncertain(
        "Listing details", [{"source": "title", "total_quantity": 300}],
        "Title and scraper pack quantities conflict.")
    ambiguous.product_name = "Amazon Hibiscus Hair Oil 100 g pack of 3"
    ambiguous.pack_count = 1
    ambiguous.total_quantity = 100
    ambiguous.total_quantity_unit = "g"
    db.add_all([verified, ambiguous])
    db.flush()
    strategy = strategist_node({"db": db, "retailer_product_id": product.id,
                                "competitors": [verified, ambiguous], "evidence_type": "LIVE"})["strategy"]
    assert strategy["market_statistics"]["comparable_offer_count"] == 1
    assert strategy["market_statistics"]["exclusion_reasons"] == {"quantity_uncertain": 1}
    assert strategy["market_reference_price"] == 180
    db.close()
    engine.dispose()


def test_formulation_and_product_type_conflicts_do_not_enter_market_statistics():
    engine, db, catalog, product = _session_with_product(quantity_value=100, quantity_unit="ml")
    alternative = _competitor(catalog, "Amazon", 150, 100, "ml")
    alternative.product_name = "Garden Brand Hibiscus Hair Oil"
    conflicting_type = _competitor(catalog, "Flipkart", 300, 100, "ml")
    conflicting_type.product_name = "Flipkart Hibiscus Hair Shampoo"
    db.add_all([alternative, conflicting_type])
    db.flush()

    strategy = strategist_node({
        "db": db, "retailer_product_id": product.id,
        "competitors": [alternative, conflicting_type], "evidence_type": "LIVE",
    })["strategy"]

    assert strategy["market_statistics"]["comparable_offer_count"] == 1
    assert strategy["market_statistics"]["median_price"] == 150
    assert strategy["market_statistics"]["exclusion_reasons"] == {"incompatible_product": 1}
    db.close()
    engine.dispose()


@pytest.mark.parametrize("title", [
    "Rosemary Essential Oil 250 ml",
    "Rosemary Hair Oil 250 ml",
    "Rosemary Hair Serum Kit 250 ml",
    "Rosemary Hair Serum Combo Set 250 ml",
])
def test_rosemary_serum_rejects_non_comparable_product_forms(title):
    quality, reason = classify_candidate("Rosemary Hair Serum", title)
    assert quality == "rejected"
    assert reason


def test_rosemary_hair_serum_accepts_high_price_genuine_serum():
    quality, _ = classify_candidate("Rosemary Hair Serum", "Rosemary Hair Serum 250 ml")
    assert quality in {"exact", "alternative"}


def test_scraped_description_mentions_do_not_override_listing_product_form():
    quality, _ = classify_candidate(
        "Rosemary Hair Serum", "Rosemary Hair Serum 30 ml",
        candidate_details="Use with rosemary essential oil. Hair serum kit instructions included.",
    )
    assert quality in {"exact", "alternative"}


def test_legacy_market_average_uses_target_pack_and_excludes_uncertain_offers():
    engine, db, catalog, product = _session_with_product(quantity_value=100, quantity_unit="ml")
    comparable = _competitor(catalog, "Amazon", 200, 200, "ml")
    uncertain = _competitor(catalog, "Flipkart", 900, 100, "ml")
    uncertain.product_details = mark_quantity_uncertain(
        "Hibiscus Hair Oil", [{"source": "title"}], "Conflicting pack quantity.")
    db.add_all([comparable, uncertain])
    db.flush()

    stats = calculate_market_average(db, catalog.id, product)

    assert stats["count"] == 1
    assert stats["average"] == 100
    assert stats["minimum"] == stats["maximum"] == 100
    assert stats["exclusion_reasons"] == {"quantity_uncertain": 1}
    db.close()
    engine.dispose()
