import asyncio
import pytest

from scraping.scout import ScoutScraper
import scraping.scout as scout_module
from scraping.base_scraper import BaseScraper


@pytest.mark.asyncio
async def test_scout_runs_requested_marketplace_only(monkeypatch):
    scout = ScoutScraper(max_retries=0)
    called = []

    async def amazon(*args, **kwargs):
        called.append("Amazon")
        return {"platform": "Amazon", "status": "SUCCESS", "products": []}

    async def unexpected(*args, **kwargs):
        raise AssertionError("an unrequested marketplace was started")

    monkeypatch.setattr(scout, "_run_amazon", amazon)
    monkeypatch.setattr(scout, "_run_flipkart", unexpected)
    monkeypatch.setattr(scout, "_run_myntra", unexpected)
    monkeypatch.setattr(scout, "_run_meesho", unexpected)

    result = await asyncio.wait_for(scout.get_platform_statuses("hibiscus hair oil", max_products=1, platforms=["Amazon"]), timeout=2)

    assert called == ["Amazon"]
    assert result["platform_statuses"] == {"Amazon": "SUCCESS"}
    assert result["total_products"] == 0


@pytest.mark.asyncio
async def test_scout_default_fallback_order_stops_after_first_success(monkeypatch):
    scout = ScoutScraper(max_retries=0)
    called = []

    async def success(platform):
        async def run(*args, **kwargs):
            called.append(platform)
            return {"platform": platform, "status": "SUCCESS", "products": [{
                "platform": platform, "product_title": "Hibiscus hair oil", "product_url": f"https://{platform.casefold()}.test/p/1",
                "price": 100, "availability": True, "product_details": [],
            }]}
        return run

    for platform, attribute in (("Myntra", "_run_myntra"), ("Meesho", "_run_meesho"),
                                ("Amazon", "_run_amazon"), ("Flipkart", "_run_flipkart")):
        monkeypatch.setattr(scout, attribute, await success(platform))

    result = await asyncio.wait_for(scout.get_platform_statuses("hibiscus hair oil", max_products=1), timeout=2)

    assert called == ["Myntra"]
    assert result["platform_statuses"] == {"Myntra": "SUCCESS"}
    assert result["total_products"] == 1


def test_navigation_failure_message_keeps_the_underlying_exception():
    scraper = BaseScraper()
    scraper.search_result_status = "NETWORK_ERROR"
    scraper.last_navigation_error = "Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR"

    assert "net::ERR_HTTP2_PROTOCOL_ERROR" in scraper.search_failure_message()


@pytest.mark.asyncio
async def test_amazon_uses_its_own_bounded_timeout(monkeypatch):
    scout = ScoutScraper(max_retries=0)
    monkeypatch.setattr(scout_module, "PLATFORM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(scout_module, "AMAZON_PLATFORM_TIMEOUT_SECONDS", 0.1)

    async def amazon(*args, **kwargs):
        await asyncio.sleep(0.03)
        return {"platform": "Amazon", "status": "SUCCESS", "products": [{
            "platform": "Amazon", "product_title": "Hibiscus hair oil",
            "product_url": "https://amazon.test/p/1", "price": 100,
            "availability": None, "product_details": [],
        }]}

    monkeypatch.setattr(scout, "_run_amazon", amazon)
    result = await scout.get_platform_statuses("hibiscus hair oil", max_products=1, platforms=["Amazon"])

    assert result["platform_statuses"] == {"Amazon": "SUCCESS"}
    assert result["total_products"] == 1
