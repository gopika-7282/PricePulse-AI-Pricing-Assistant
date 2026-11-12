from app.services.market_statistics import calculate_market_statistics, market_confidence


def test_iqr_range_flags_but_does_not_remove_a_valid_outlier():
    stats = calculate_market_statistics([
        {"marketplace": "Amazon", "price": 100},
        {"marketplace": "Flipkart", "price": 110},
        {"marketplace": "Myntra", "price": 120},
        {"marketplace": "Amazon", "price": 130},
        {"marketplace": "Flipkart", "price": 900},
    ])

    assert stats["median_price"] == 120
    assert stats["mean_price"] == 272
    assert stats["typical_price_range_low"] == 110
    assert stats["typical_price_range_high"] == 130
    assert stats["observed_minimum_price"] == 100
    assert stats["observed_maximum_price"] == 900
    assert stats["comparable_offer_count"] == 5
    assert stats["distinct_marketplace_count"] == 3
    assert stats["outlier_count"] == 1
    assert stats["outlier_offers"][0]["price"] == 900
    assert "retained" in stats["outlier_offers"][0]["reason"]


def test_invalid_and_ineligible_offers_are_counted_by_reason():
    stats = calculate_market_statistics([
        {"marketplace": "Amazon", "price": 100},
        {"marketplace": "Flipkart", "price": float("nan")},
        {"marketplace": "Meesho", "exclusion_reason": "quantity_uncertain"},
        {"marketplace": "Amazon", "price": -4},
    ])

    assert stats["comparable_offer_count"] == 1
    assert stats["exclusion_count"] == 3
    assert stats["exclusion_reasons"] == {"invalid_price": 2, "quantity_uncertain": 1}


def test_market_confidence_is_low_for_sparse_samples_and_stale_data_is_capped():
    assert market_confidence(1, 1) == 0.30
    assert market_confidence(3, 2) == 0.65
    assert market_confidence(8, 3, stale=True) == 0.55
