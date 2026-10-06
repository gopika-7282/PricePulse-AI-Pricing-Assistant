from sqlalchemy.orm import Session
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.services.competitor_service import has_fresh_competitor_data
from app.services.scraping_service import scrape_product
from app.services.pricing_service import calculate_price_analysis, generate_recommendation
from app.services.matching_service import find_catalog_match, find_existing_product
from app.config import SCRAPE_FRESHNESS_THRESHOLD_DAYS
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


def create_product_catalog(
    db: Session,
    name: str,
    category: str,
    brand: str,
    product_details: str
) -> ProductCatalog:
    """Create a new ProductCatalog entry in PostgreSQL."""
    db_catalog = ProductCatalog(
        name=name,
        category=category,
        brand=brand,
        product_details=product_details,
    )
    db.add(db_catalog)
    db.commit()
    db.refresh(db_catalog)

    logger.info(f"[CATALOG_CREATED] id={db_catalog.id} name='{name}'")
    return db_catalog



def find_existing_product(
    db: Session,
    name: str,
    category: str = "",
    brand: str = "",
    product_details: str = "",
) -> Optional[ProductCatalog]:
    """Find existing catalog product using matching service."""
    return find_catalog_match(
        db=db,
        name=name,
        category=category,
        brand=brand,
        details=product_details,
    )


def create_retailer_product(
    db: Session,
    user_id: int,
    name: str,
    category: str,
    brand: str,
    product_details: str,
    cost_price: float,
    stock_quantity: int,
    minimum_profit_margin: float
) -> RetailerProduct:
    logger.info(f"[PRODUCT] Incoming product: name='{name}' category='{category}' brand='{brand}'")

    # Intelligent Lifecycle Decision
    from app.services.catalog_lifecycle_service import determine_product_lifecycle
    
    lifecycle_result = determine_product_lifecycle(
        db=db,
        product_name=name,
        category=category,
        brand=brand,
        product_details=product_details
    )
    
    decision = lifecycle_result["decision"]
    catalog_product_id = lifecycle_result["catalog_product_id"]
    
    if decision == "CREATE_NEW":
        catalog_product = create_product_catalog(db, name, category, brand, product_details)
        needs_scraping = True
    elif decision == "REFRESH_EXISTING":
        catalog_product = get_catalog_product_by_id(db, catalog_product_id)
        needs_scraping = True
    elif decision == "REUSE_EXISTING":
        catalog_product = get_catalog_product_by_id(db, catalog_product_id)
        needs_scraping = False
    else:
        raise ValueError(f"Unknown lifecycle decision: {decision}")

    if needs_scraping:
        scrape_product(db, catalog_product)
        if decision == "REFRESH_EXISTING":
            logger.info(f"[COMPETITOR_REFRESH_COMPLETED] id={catalog_product.id}")

    # Step 4: Create retailer_product linked to this user and catalog entry
    db_retailer_product = RetailerProduct(
        user_id=user_id,
        catalog_product_id=catalog_product.id,
        cost_price=cost_price,
        stock_quantity=stock_quantity,
        minimum_profit_margin=minimum_profit_margin
    )
    db.add(db_retailer_product)
    db.commit()
    db.refresh(db_retailer_product)

    # Step 5: Price Analysis and Recommendation
    # Failure here must NOT rollback already-committed competitor data
    logger.info(f"[RECOMMENDATION] Started: retailer_product_id={db_retailer_product.id}")
    try:
        calculate_price_analysis(db, db_retailer_product.id)
        generate_recommendation(db, db_retailer_product.id)
        logger.info(f"[RECOMMENDATION] Succeeded: retailer_product_id={db_retailer_product.id}")
    except Exception as e:
        logger.error(
            f"[RECOMMENDATION] Failed: retailer_product_id={db_retailer_product.id} error='{e}'"
        )
        # Competitor data and retailer product already committed — do NOT rollback

    return db_retailer_product


def get_retailer_products(db: Session, retailer_id: int) -> List[RetailerProduct]:
    return db.query(RetailerProduct).filter(RetailerProduct.user_id == retailer_id).all()


def get_retailer_product_by_id(db: Session, product_id: int, user_id: int) -> Optional[RetailerProduct]:
    return db.query(RetailerProduct).filter(
        RetailerProduct.id == product_id,
        RetailerProduct.user_id == user_id
    ).first()


def update_retailer_product(
    db: Session,
    product_id: int,
    user_id: int,
    cost_price: float,
    stock_quantity: int,
    minimum_profit_margin: float
) -> Optional[RetailerProduct]:
    product = get_retailer_product_by_id(db, product_id, user_id)
    if product:
        product.cost_price = cost_price
        product.stock_quantity = stock_quantity
        product.minimum_profit_margin = minimum_profit_margin
        db.commit()
        db.refresh(product)

        # Regenerate recommendation because costs moved
        calculate_price_analysis(db, product.id)
        generate_recommendation(db, product.id)

    return product


def delete_retailer_product(db: Session, product_id: int, user_id: int) -> bool:
    product = get_retailer_product_by_id(db, product_id, user_id)
    if product:
        db.delete(product)
        db.commit()
        return True
    return False


def get_all_catalog_products(db: Session) -> List[ProductCatalog]:
    return db.query(ProductCatalog).all()


def get_catalog_product_by_id(db: Session, catalog_id: int) -> Optional[ProductCatalog]:
    return db.query(ProductCatalog).filter(ProductCatalog.id == catalog_id).first()
