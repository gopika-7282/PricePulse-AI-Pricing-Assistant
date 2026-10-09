import pytest

from scraping.amazon_scraper import AmazonScraper, HARD_MAX_PRODUCTS, select_amazon_candidates


def candidate(idx, title=None, asin=None, price=125.0):
    asin = asin or f"B{idx:09d}"
    return {
        "platform": "Amazon",
        "product_title": title or f"Hibiscus Herbal Hair Oil Variant {idx}",
        "product_url": f"https://www.amazon.in/dp/{asin}",
        "price": price,
        "asin": asin,
        "ranking": idx,
        "rating": None,
        "availability": None,
        "product_details": [],
    }


@pytest.mark.parametrize("count, expected", [(48, 10), (15, 10), (10, 10), (7, 7), (1, 1), (0, 0)])
def test_filters_and_validates_entire_pool_before_hard_cap(count, expected):
    raw = [candidate(i) for i in range(1, count + 1)]

    selected, counts = select_amazon_candidates(raw, "Hibiscus Hair Oil", "Hair Care", 10)

    assert HARD_MAX_PRODUCTS == 10
    assert len(selected) == expected
    assert counts["parsed"] == count
    assert counts["unique"] == count
    assert counts["validated"] == count
    assert counts["relevant"] == count
    assert counts["selected"] == expected


def test_deduplicates_by_asin_and_rejects_invalid_and_unrelated_candidates():
    raw = [
        candidate(1, asin="B000000001"),
        candidate(2, title="Hibiscus Shampoo", asin="B000000002"),
        candidate(3, price=0, asin="B000000003"),
        candidate(4, asin="B000000001"),  # repeated ASIN, different card position
    ]

    selected, counts = select_amazon_candidates(raw, "Hibiscus Hair Oil", "Hair Care", 10)

    assert len(selected) == 1
    assert selected[0]["asin"] == "B000000001"
    assert counts == {
        "parsed": 4, "unique": 3, "validated": 2,
        "relevant": 1, "ranked": 1, "selected": 1,
    }


@pytest.mark.asyncio
async def test_detail_failure_on_second_candidate_keeps_card_and_continues():
    scraper = AmazonScraper(max_retries=0)
    candidates = [candidate(i) for i in range(1, 5)]

    async def fetch(page, cand, idx, total):
        if idx == 2:
            raise TimeoutError("detail request timed out")
        if idx == 3:
            return {}  # Missing detail title also falls back to the valid search card.
        return {**cand, "product_title": cand["product_title"] + " enriched"}

    scraper._fetch_detail_page = fetch
    result = await scraper._enrich_selected_candidates(object(), candidates)

    assert len(result) == 4
    assert result[0]["product_title"].endswith("enriched")
    assert result[1]["product_title"] == candidates[1]["product_title"]
    assert result[2]["product_title"] == candidates[2]["product_title"]
    assert result[3]["product_title"].endswith("enriched")
