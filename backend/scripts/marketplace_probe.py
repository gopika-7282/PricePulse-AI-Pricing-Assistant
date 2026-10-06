"""Run a bounded, sequential read-only live scrape for each supported marketplace."""
import asyncio
import json

from scraping.scout import ScoutScraper


async def main() -> None:
    scout = ScoutScraper(max_retries=1)
    tasks = (
        ("Amazon", scout._run_amazon),
        ("Flipkart", scout._run_flipkart),
        ("Myntra", scout._run_myntra),
        ("Meesho", scout._run_meesho),
    )
    for platform, runner in tasks:
        try:
            result = await runner("Aloe Vera Gel", "Personal Care", "Aloe gel for skin use", 1)
        except Exception as exc:
            result = {"platform": platform, "status": "NETWORK_ERROR", "products": [], "error": type(exc).__name__}
        summary = {
            "platform": platform,
            "status": result.get("status"),
            "error": result.get("error"),
            "products": [
                {"product_name": item.get("product_title"), "price": item.get("price"), "product_url": item.get("product_url")}
                for item in result.get("products", [])[:1]
            ],
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
