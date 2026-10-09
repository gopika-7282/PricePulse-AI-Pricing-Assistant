from app.utils.quantity import extract_quantity, normalize_quantity, price_at_target_pack


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
