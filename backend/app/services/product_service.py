from sqlalchemy.orm import Session
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
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


def find_existing_product(db: Session, name: str, category: str = "", brand: str = "") -> Optional[ProductCatalog]:
    """Compatibility lookup that delegates identity semantics to the canonical Qwen service.

    The retailer pricing workflow does not call this helper; its LangGraph identity
    and catalog-decision nodes remain authoritative for create/edit lifecycle.
    """
    from app.services.product_identity_service import evaluate_product_identity

    result = evaluate_product_identity(db, name, "", category, brand)
    if result.decision.value != "MATCH" or not result.matched_catalog_id:
        return None
    return db.query(ProductCatalog).filter(ProductCatalog.id == result.matched_catalog_id).first()



def create_retailer_product(
    db: Session,
    user_id: int,
    name: str,
    category: str,
    brand: str,
    product_details: str,
    cost_price: float,
    stock_quantity: int,
    minimum_profit_margin: float,
    quantity_value: float | None = None,
    quantity_unit: str | None = None,
) -> RetailerProduct:
    logger.info(f"[PRODUCT] Incoming product: name='{name}' category='{category}' brand='{brand}'")

    # The non-null catalog FK needs a staging relationship. LangGraph's
    # identity/catalog nodes make the final decision and replace it as needed.
    catalog_product = ProductCatalog(
        name=name, category=category, brand=brand, product_details=product_details,
    )
    db.add(catalog_product)
    db.flush()

    # Step 4: Create retailer_product linked to this user and catalog entry
    db_retailer_product = RetailerProduct(
        user_id=user_id,
        catalog_product_id=catalog_product.id,
        name_override=name,
        category_override=category,
        brand_override=brand,
        product_details_override=product_details,
        catalog_identity_pending=True,
        catalog_identity_staging=True,
        cost_price=cost_price,
        stock_quantity=stock_quantity,
        quantity_value=quantity_value,
        quantity_unit=quantity_unit,
        minimum_profit_margin=minimum_profit_margin
    )
    db.add(db_retailer_product)
    db.commit()
    db.refresh(db_retailer_product)

    # Recommendation generation is an explicit authenticated workflow action;
    # product creation never inserts an unchecked recommendation.
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
    name: str,
    category: str,
    brand: str,
    product_details: str,
    cost_price: float,
    stock_quantity: int,
    minimum_profit_margin: float,
    quantity_value: float | None = None,
    quantity_unit: str | None = None,
) -> Optional[RetailerProduct]:
    product = get_retailer_product_by_id(db, product_id, user_id)
    if product:
        product.catalog_identity_pending = True
        product.catalog_identity_staging = False
        product.name_override = name
        product.category_override = category
        product.brand_override = brand
        product.product_details_override = product_details
        product.cost_price = cost_price
        product.stock_quantity = stock_quantity
        product.quantity_value = quantity_value
        product.quantity_unit = quantity_unit
        product.minimum_profit_margin = minimum_profit_margin
        # Existing analysis belongs to the previous inputs and must not appear
        # as current after a retailer changes product or pricing details.
        product.recommendation.clear()
        product.price_analysis.clear()
        db.commit()
        db.refresh(product)

    return product


def delete_retailer_product(db: Session, product_id: int, user_id: int) -> bool:
    product = get_retailer_product_by_id(db, product_id, user_id)
    if product:
        pending_catalog = product.catalog_product if product.catalog_identity_staging else None
        db.delete(product)
        db.flush()
        if pending_catalog is not None:
            from app.models.competitor_product import CompetitorProduct
            other_retailer = db.query(RetailerProduct.id).filter_by(catalog_product_id=pending_catalog.id).first()
            observed = db.query(CompetitorProduct.id).filter_by(catalog_product_id=pending_catalog.id).first()
            if not other_retailer and not observed:
                db.delete(pending_catalog)
        db.commit()
        return True
    return False


def get_all_catalog_products(db: Session) -> List[ProductCatalog]:
    return db.query(ProductCatalog).all()


def get_catalog_product_by_id(db: Session, catalog_id: int) -> Optional[ProductCatalog]:
    return db.query(ProductCatalog).filter(ProductCatalog.id == catalog_id).first()
