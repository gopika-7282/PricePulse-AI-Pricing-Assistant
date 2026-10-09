"""
competitor_service.py
=====================
Competitor product management, stable-identity upsert, and 7-day freshness tracking.

Key guarantees:
1. Stable Competitor Identity:
   Matches existing competitor records by (catalog_product_id, platform_name, normalized_product_url).
   If URL is missing, falls back to product_name.
2. Upsert Behavior:
   Updates existing CompetitorProduct in-place without duplicating rows after freshness expiry.
3. Price History:
   CompetitorProduct holds the latest current price.
   CompetitorPriceHistory preserves price transitions over time.
4. Strong 7-Day Freshness:
   Decisions driven strictly by persisted database UTC timestamps.
5. Structured logging tags: [DB], [FRESHNESS], [HISTORY].
"""

from typing import List, Optional
from datetime import datetime, timezone, timedelta
import logging
import urllib.parse
from sqlalchemy.orm import Session

from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory
from app.config import SCRAPE_FRESHNESS_THRESHOLD_DAYS

logger = logging.getLogger(__name__)


def normalize_competitor_url(raw_url: Optional[str]) -> Optional[str]:
    """
    Clean and normalize competitor URLs:
    - Strips transient search/tracking query parameters (e.g. ssid, otracker, iid, qH, etc.)
    - Preserves canonical path (/product-slug/p/itm...) and pid parameter for Flipkart.
    - Normalizes schemes and hosts.
    """
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    if not raw_url:
        return None

    if raw_url.startswith("/"):
        raw_url = f"https://www.flipkart.com{raw_url}"

    try:
        parsed = urllib.parse.urlparse(raw_url)
        qs = urllib.parse.parse_qs(parsed.query)

        # For Flipkart, preserve 'pid' if present
        pid = qs.get("pid", [None])[0]
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if pid:
            clean_url += f"?pid={pid}"
        return clean_url
    except Exception:
        return raw_url


def save_price_history(
    db: Session,
    competitor_product_id: int,
    price: float,
    scraped_at: Optional[datetime] = None
) -> CompetitorPriceHistory:
    """Save an entry in the competitor price history table."""
    now = scraped_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    history_entry = CompetitorPriceHistory(
        competitor_product_id=competitor_product_id,
        price=price,
        scraped_at=now,
    )
    db.add(history_entry)
    db.flush()
    return history_entry


def upsert_competitor_product(
    db: Session,
    catalog_product_id: int,
    platform_name: str,
    product_name: str,
    price: float,
    product_url: Optional[str] = None,
    product_details: Optional[str] = None,
    rating: Optional[float] = None,
    availability: Optional[bool] = None,
    quantity_value: Optional[float] = None,
    quantity_unit: Optional[str] = None,
    pack_count: Optional[int] = None,
    total_quantity: Optional[float] = None,
    total_quantity_unit: Optional[str] = None,
) -> CompetitorProduct:
    """
    Upsert competitor product according to stable identity:
    (catalog_product_id, platform_name, normalized_product_url).

    If existing record found:
      - Update fields in-place: product_name, product_details, price, rating, availability, scraped_at, updated_at.
      - Record new price history entry if price changed.
      - Do NOT create another CompetitorProduct row.
    If not found:
      - Insert new CompetitorProduct row.
      - Record initial price history entry.
    """
    clean_url = normalize_competitor_url(product_url)
    now = datetime.now(timezone.utc)

    logger.info(
        f"[DB] Competitor lookup: catalog_id={catalog_product_id} "
        f"platform='{platform_name}' url='{clean_url or product_name}'"
    )

    # 1. Search for existing competitor by stable identity
    db_comp: Optional[CompetitorProduct] = None
    if clean_url:
        # First try exact normalized URL match
        db_comp = (
            db.query(CompetitorProduct)
            .filter(
                CompetitorProduct.catalog_product_id == catalog_product_id,
                CompetitorProduct.platform_name == platform_name,
                CompetitorProduct.product_url == clean_url,
            )
            .first()
        )

        # Fallback: check if uncleaned URL in DB matches the base path / pid
        if not db_comp:
            candidates = (
                db.query(CompetitorProduct)
                .filter(
                    CompetitorProduct.catalog_product_id == catalog_product_id,
                    CompetitorProduct.platform_name == platform_name,
                )
                .all()
            )
            for cand in candidates:
                if normalize_competitor_url(cand.product_url) == clean_url:
                    db_comp = cand
                    break

    if not db_comp:
        # Fallback to product_name match if no URL available
        db_comp = (
            db.query(CompetitorProduct)
            .filter(
                CompetitorProduct.catalog_product_id == catalog_product_id,
                CompetitorProduct.platform_name == platform_name,
                CompetitorProduct.product_name == product_name,
            )
            .first()
        )

    # 2. Update existing or insert new
    if db_comp:
        logger.info(
            f"[DB] Existing competitor found: id={db_comp.id} "
            f"(previous_price={db_comp.price}, new_price={price})"
        )

        price_changed = (db_comp.price != price)
        if price_changed:
            logger.info(
                f"[HISTORY] Price history updated: competitor_id={db_comp.id} "
                f"{db_comp.price} -> {price}"
            )
            save_price_history(db, db_comp.id, price, now)

        # Update competitor fields
        db_comp.product_name = product_name
        db_comp.product_url = clean_url
        # Only overwrite product_details when new value is non-empty; preserve existing
        # non-empty details to prevent a blocked/failed detail page from erasing known specs.
        if product_details:
            db_comp.product_details = product_details
        db_comp.price = price
        db_comp.quantity_value = quantity_value or db_comp.quantity_value
        db_comp.quantity_unit = quantity_unit or db_comp.quantity_unit
        db_comp.pack_count = pack_count or db_comp.pack_count
        db_comp.total_quantity = total_quantity or db_comp.total_quantity
        db_comp.total_quantity_unit = total_quantity_unit or db_comp.total_quantity_unit
        db_comp.rating = rating
        db_comp.availability = availability
        db_comp.scraped_at = now
        db_comp.updated_at = now

        db.flush()
        logger.info(f"[DB] Competitor updated: id={db_comp.id}")
        return db_comp
    else:
        # Insert new competitor product
        db_comp = CompetitorProduct(
            catalog_product_id=catalog_product_id,
            platform_name=platform_name,
            product_name=product_name,
            product_url=clean_url,
            product_details=product_details,
            price=price,
            quantity_value=quantity_value, quantity_unit=quantity_unit,
            pack_count=pack_count, total_quantity=total_quantity,
            total_quantity_unit=total_quantity_unit,
            rating=rating,
            availability=availability,
            scraped_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(db_comp)
        db.flush()

        logger.info(f"[DB] New competitor inserted: id={db_comp.id}")
        save_price_history(db, db_comp.id, price, now)
        logger.info(f"[HISTORY] Price history updated: competitor_id={db_comp.id} initial price={price}")
        return db_comp


def update_competitor_price(db: Session, competitor_product_id: int, new_price: float) -> CompetitorProduct:
    """Manually update competitor price and record price history."""
    db_comp = db.query(CompetitorProduct).filter(CompetitorProduct.id == competitor_product_id).first()
    if db_comp and db_comp.price != new_price:
        now = datetime.now(timezone.utc)
        logger.info(
            f"[HISTORY] Price history updated: competitor_id={db_comp.id} "
            f"{db_comp.price} -> {new_price}"
        )
        save_price_history(db, db_comp.id, new_price, now)
        db_comp.price = new_price
        db_comp.scraped_at = now
        db_comp.updated_at = now
        db.flush()
        logger.info(f"[DB] Competitor updated: id={db_comp.id}")

    return db_comp


def get_latest_competitor_prices(db: Session, catalog_product_id: int) -> List[CompetitorProduct]:
    return db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == catalog_product_id).all()


def get_all_competitor_products(db: Session) -> List[CompetitorProduct]:
    return db.query(CompetitorProduct).all()


def get_competitor_product_by_id(db: Session, competitor_id: int) -> Optional[CompetitorProduct]:
    return db.query(CompetitorProduct).filter(CompetitorProduct.id == competitor_id).first()


def get_competitor_price_history(db: Session, competitor_product_id: int) -> List[CompetitorPriceHistory]:
    return (
        db.query(CompetitorPriceHistory)
        .filter(CompetitorPriceHistory.competitor_product_id == competitor_product_id)
        .order_by(CompetitorPriceHistory.scraped_at.desc(), CompetitorPriceHistory.created_at.desc())
        .all()
    )


def is_competitor_data_fresh(scraped_at: Optional[datetime], threshold_days: int = 7) -> bool:
    """
    Checks whether the competitor product data was scraped recently based on the threshold.
    NOTE: Kept for backward compatibility. Use has_fresh_competitor_data() for new code.
    """
    if not scraped_at:
        return False
    current_time = datetime.now(timezone.utc)
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    age = current_time - scraped_at
    return age <= timedelta(days=threshold_days)


def get_competitor_freshness(db: Session, catalog_product_id: int) -> dict:
    """
    Single source of truth for scraping freshness decisions based on persisted DB timestamps.

    Queries competitor_products.scraped_at (most recent row for catalog_product_id).
    Product catalog timestamps and workflow execution time are not freshness inputs.
    """
    latest = (
        db.query(CompetitorProduct)
        .filter(CompetitorProduct.catalog_product_id == catalog_product_id)
        .order_by(CompetitorProduct.scraped_at.desc())
        .first()
    )

    if not latest:
        return {"status": "NO_EVIDENCE", "latest_observation_at": None, "age_days": None}

    scraped_at = latest.scraped_at
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    age = now - scraped_at
    age_days = age.total_seconds() / 86400.0

    status = "FRESH" if age <= timedelta(days=SCRAPE_FRESHNESS_THRESHOLD_DAYS, seconds=5) else "STALE"
    logger.info("[FRESHNESS] catalog_product_id=%s status=%s age_days=%.2f threshold_days=%s",
                catalog_product_id, status, age_days, SCRAPE_FRESHNESS_THRESHOLD_DAYS)
    return {"status": status, "latest_observation_at": scraped_at, "age_days": age_days}


def has_fresh_competitor_data(db: Session, catalog_product_id: int, threshold_days: Optional[int] = None) -> bool:
    """Backward-compatible boolean API; configured freshness is used by production callers."""
    if threshold_days is None or threshold_days == SCRAPE_FRESHNESS_THRESHOLD_DAYS:
        return get_competitor_freshness(db, catalog_product_id)["status"] == "FRESH"
    latest = db.query(CompetitorProduct).filter(
        CompetitorProduct.catalog_product_id == catalog_product_id
    ).order_by(CompetitorProduct.scraped_at.desc()).first()
    return bool(latest and is_competitor_data_fresh(latest.scraped_at, threshold_days))
