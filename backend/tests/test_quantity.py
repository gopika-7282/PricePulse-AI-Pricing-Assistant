import pytest

from app.utils.quantity import (
    extract_quantity, mark_quantity_provenance, normalize_quantity,
    price_at_target_pack, reconcile_quantity, strip_quantity_marker,
)


def test_explicit_pack_quantities_and_pack_counts():
    assert extract_quantity("Multani Mitti 100 g") == {
        "quantity_value": 100.0, "quantity_unit": "g", "pack_count": 1,
        "total_quantity": 100.0, "total_quantity_unit": "g",
    }
    assert extract_quantity("Hair Oil 2 x 100 ml") == {
        "quantity_value": 100.0, "quantity_unit": "ml", "pack_count": 2,
        "total_quantity": 200.0, "total_quantity_unit": "ml",
    }
    assert extract_quantity("Hair Oil 1 L") ["quantity_value"] == 1.0


def test_normalization_is_dimension_safe():
    assert normalize_quantity(1, "kg") == (1000.0, "mass")
    assert normalize_quantity(1, "L") == (1000.0, "volume")
    assert normalize_quantity(100, "g")[1] != normalize_quantity(100, "ml")[1]
    assert normalize_quantity(3, "piece") is None


def test_target_pack_price_respects_pack_count_and_dimensions():
    assert price_at_target_pack(200, 100, "g", 100, "g", pack_count=2) == 100
    assert price_at_target_pack(200, 100, "g", 100, "g", pack_count=2,
                                total_quantity=200, total_quantity_unit="g") == 100
    assert price_at_target_pack(100, 100, "g", 100, "ml") is None


def test_unknown_quantity_is_not_invented():
    assert extract_quantity("Natural face powder") is None


def test_quantity_provenance_marker_round_trips_json_list_and_handles_invalid_input():
    details = '["200 ml bottle", "Herbal ingredients"]'
    marker = mark_quantity_provenance(details, [{"source": "title", "total_quantity": 200}])
    assert strip_quantity_marker(marker) == details
    assert strip_quantity_marker(details) == details
    assert strip_quantity_marker(None) is None
    assert strip_quantity_marker(details + "\n[[PRICEPULSE_QUANTITY_EVIDENCE:invalid") == details


@pytest.mark.parametrize(("text", "value", "unit", "count", "total"), [
    ("Aloe gel 100 g pack of 3", 100, "g", 3, 300),
    ("Aloe gel 500 g × 4", 500, "g", 4, 2000),
    ("Hair oil 2 x 100 g", 100, "g", 2, 200),
    ("Conditioner 200 ml pack of 2", 200, "ml", 2, 400),
    ("Bottle 1 kg", 1, "kg", 1, 1),
])
def test_common_pack_formats_preserve_item_count_and_total(text, value, unit, count, total):
    parsed = extract_quantity(text)
    assert parsed["quantity_value"] == value
    assert parsed["quantity_unit"] == unit
    assert parsed["pack_count"] == count
    assert parsed["total_quantity"] == total


def test_title_and_scraper_conflict_is_ambiguous_with_source_evidence():
    result = reconcile_quantity("Aloe gel 100 g pack of 3", scraper_metadata={
        "quantity_value": 100, "quantity_unit": "g", "pack_count": 1,
        "total_quantity": 100, "total_quantity_unit": "g",
    })
    assert result["status"] == "ambiguous"
    assert {item["source"] for item in result["evidence"]} == {"title", "scraper_metadata"}
    assert "quantity_value" not in result


def test_details_conflict_and_gross_weight_are_not_used_as_net_quantity():
    conflict = reconcile_quantity("Aloe gel 100 g", details="Net quantity: 150 g")
    assert conflict["status"] == "ambiguous"
    gross = reconcile_quantity("Aloe gel", details="Gross weight: 1.1 kg; Net quantity: 1 kg")
    assert gross["status"] == "resolved"
    assert gross["total_quantity"] == 1
    assert gross["evidence"][0]["gross_weight"] == {"value": 1.1, "unit": "kg"}


def test_internally_conflicting_scraper_pack_fields_are_ambiguous():
    result = reconcile_quantity(scraper_metadata={
        "quantity_value": 100, "quantity_unit": "g", "pack_count": 3,
        "total_quantity": 100, "total_quantity_unit": "g",
    })
    assert result["status"] == "ambiguous"


def test_extra_quantity_in_title_is_not_ignored_when_pack_expression_exists():
    result = reconcile_quantity(title="Aloe gel 100 g pack of 3 plus 50 g free")
    assert result["status"] == "ambiguous"


def test_uncertain_and_mass_volume_quantities_cannot_normalize():
    assert price_at_target_pack(300, 300, "g", 100, "g", 3,
                                quantity_uncertain=True) is None
    assert price_at_target_pack(300, 300, "g", 200, "ml", 2) is None
