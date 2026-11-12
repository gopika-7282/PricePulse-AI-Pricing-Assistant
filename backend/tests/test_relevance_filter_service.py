from app.services import relevance_filter_service as relevance


def test_exact_match_and_alternative_brand_are_distinguished():
    relevance._RELEVANCE_CACHE.clear()
    result = relevance.filter_candidate_products("Aloe Vera Gel", "Skin care", [
        {"product_title": "Aloe Vera Gel"},
        {"product_title": "GardenPure Aloe Vera Gel"},
        {"product_title": "Aloevera Gel"},
    ])
    assert [item["match_quality"] for item in result] == ["exact", "alternative", "exact"]
    relevance._RELEVANCE_CACHE.clear()


def test_meaningful_formulation_and_product_type_conflicts_are_rejected(monkeypatch):
    relevance._RELEVANCE_CACHE.clear()
    monkeypatch.setattr(relevance, "generate_structured", lambda *a, **k: {
        "success": True, "data": {"results": [{"index": 0, "relevant": True}]}
    })
    result = relevance.filter_candidate_products("Aloe Vera Gel", "Skin care", [
        {"product_title": "Aloe Ashwagandha Gel"},
        {"product_title": "Aloe Vera Cream"},
    ])
    assert result == []
    relevance._RELEVANCE_CACHE.clear()


def test_candidate_details_formulation_conflict_is_not_overridden_by_llm(monkeypatch):
    relevance._RELEVANCE_CACHE.clear()
    monkeypatch.setattr(relevance, "generate_structured", lambda *a, **k: {
        "success": True, "data": {"results": [{"index": 0, "relevant": True}]}
    })
    result = relevance.filter_candidate_products(
        "Aloe Vera Gel", "Skin care",
        [{"product_title": "Aloe Vera Gel", "product_details": "Contains ashwagandha"}],
    )
    assert result == []
    relevance._RELEVANCE_CACHE.clear()


def test_unproven_candidate_is_excluded_even_when_llm_says_relevant(monkeypatch):
    relevance._RELEVANCE_CACHE.clear()
    monkeypatch.setattr(relevance, "generate_structured", lambda *a, **k: {
        "success": True, "data": {"results": [{"index": 0, "relevant": True}]}
    })
    assert relevance.filter_candidate_products("Aloe Vera Gel", "Skin care", [
        {"product_title": "Gel"},
    ]) == []
    relevance._RELEVANCE_CACHE.clear()
