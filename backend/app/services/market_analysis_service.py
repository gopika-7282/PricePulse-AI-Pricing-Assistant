"""Evidence-only competitor context for chatbot current and historical questions."""

from datetime import datetime, timedelta, timezone

from app.config import SCRAPE_FRESHNESS_THRESHOLD_DAYS
from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory


def classify_market_question(question: str, history_text: str = "") -> str | None:
    """Identify time-sensitive market questions without constraining Qwen's language understanding."""
    text = f"{history_text} {question}".casefold()
    temporal = ("current", "currently", "right now", "today", "this week", "this month",
                "recent", "last month", "last 30", "last 90", "over the last", "trend", "changed")
    market = ("market", "competitor", "competitors", "pricing", "price", "trend", "charging")
    if any(word in text for word in temporal) and any(word in text for word in market):
        return "HISTORICAL_TREND" if any(w in question.casefold() for w in ("last month", "last 30", "last 90", "over the last", "historically")) else "MARKET_ANALYSIS"
    return None


def build_market_evidence(db, catalog_product_id: int | None, intent: str | None, now=None, question: str = "") -> str:
    """Describe observed prices with timestamps and sample limits; never extrapolate unsupported trends."""
    if not intent:
        return ""
    if catalog_product_id is None:
        return ("I can analyze recent marketplace evidence stored in PricePulse for a selected product, but I do not currently have a verified external source for broader industry-wide trends. "
                "No product or resolved category is selected for a comparable marketplace lookup, so no current or historical marketplace claim can be made.")

    now = now or datetime.now(timezone.utc)
    competitors = db.query(CompetitorProduct).filter_by(catalog_product_id=catalog_product_id).all()
    fresh_cutoff = now - timedelta(days=SCRAPE_FRESHNESS_THRESHOLD_DAYS)
    fresh = []
    for row in competitors:
        scraped_at = row.scraped_at
        if scraped_at and scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)
        if scraped_at and scraped_at >= fresh_cutoff and row.availability and row.price > 0:
            fresh.append((row, scraped_at))

    lines = [f"Evidence intent: {intent}. Marketplace observations are current only within the existing {SCRAPE_FRESHNESS_THRESHOLD_DAYS}-day freshness window."]
    if fresh:
        lines.append(f"Fresh observed listings: {len(fresh)}.")
        for row, scraped_at in fresh[:20]:
            lines.append(f"Observed listing: {row.product_name}; platform {row.platform_name}; price ₹{row.price:,.2f}; scraped_at {scraped_at.isoformat()}.")
    else:
        lines.append("No marketplace listing is fresh enough to describe as current.")

    ids = [row.id for row in competitors]
    normalized_question = question.casefold()
    period_days = 90
    if "last month" in normalized_question:
        first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_last_month = first_this_month - timedelta(microseconds=1)
        start = end_last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_label = f"previous calendar month ({start.date()} through {end_last_month.date()})"
    else:
        if "last 30" in normalized_question:
            period_days = 30
        elif "last 90" in normalized_question:
            period_days = 90
        start = now - timedelta(days=period_days)
        period_label = f"last {period_days} days"
    observations = (db.query(CompetitorPriceHistory)
                    .filter(CompetitorPriceHistory.competitor_product_id.in_(ids))
                    .filter(CompetitorPriceHistory.scraped_at >= start)
                    .filter(CompetitorPriceHistory.scraped_at <= now)
                    .order_by(CompetitorPriceHistory.scraped_at.asc()).all()) if ids else []
    valid = [o for o in observations if o.price and o.price > 0]
    by_listing = {}
    for observation in valid:
        by_listing.setdefault(observation.competitor_product_id, []).append(observation)
    if not valid:
        lines.append(f"No persisted competitor price history was recorded in the {period_label}; historical direction is unavailable.")
    else:
        prices = [float(o.price) for o in valid]
        lines.append(f"Historical observations in the {period_label}: {len(valid)} across {len(by_listing)} listings; minimum ₹{min(prices):,.2f}, maximum ₹{max(prices):,.2f}, average ₹{sum(prices)/len(prices):,.2f}.")
        movers = []
        for listing_id, series in by_listing.items():
            if len(series) >= 2 and float(series[0].price) > 0:
                change = float(series[-1].price) - float(series[0].price)
                movers.append((listing_id, change, change / float(series[0].price) * 100))
        if movers:
            mean_change = sum(m[1] for m in movers) / len(movers)
            lines.append(f"Direction is calculable for {len(movers)} listing(s) with repeated observations; mean absolute change ₹{mean_change:,.2f}. This describes observed listings only, not the whole market.")
        else:
            lines.append("There is not enough repeated historical evidence to establish a price trend.")
    return "\n".join(lines)
