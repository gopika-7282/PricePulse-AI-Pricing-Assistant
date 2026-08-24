from sqlalchemy.orm import Session
from sqlalchemy import func
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from typing import List, Optional

def find_existing_product(db: Session, name: str, category: str, brand: str) -> Optional[ProductCatalog]:
    query = db.query(ProductCatalog).filter(func.lower(ProductCatalog.name) == name.lower())
    if category:
        query = query.filter(func.lower(ProductCatalog.category) == category.lower())
    if brand:
        query = query.filter(func.lower(ProductCatalog.brand) == brand.lower())
    return query.first()

def create_product_catalog(db: Session, name: str, category: str, brand: str, product_details: str) -> ProductCatalog:
    db_catalog = ProductCatalog(
        name=name,
        category=category,
        brand=brand,
        product_details=product_details
    )
    db.add(db_catalog)
    db.commit()
    db.refresh(db_catalog)
    return db_catalog

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
    # 1. & 2. Check whether product already exists in product_catalog
    catalog_product = find_existing_product(db, name, category, brand)
    
    # 3. If exists, reuse existing catalog product
    # 4. If does not exist, create new product_catalog entry
    if not catalog_product:
        catalog_product = create_product_catalog(db, name, category, brand, product_details)
        
    # 5. Create retailer_product record linked with user_id, catalog_product_id, etc.
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
    
    # 6. Return created retailer product
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
