from sqlalchemy.orm import Session
from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory
from typing import List, Optional
from datetime import datetime, timezone

def save_price_history(db: Session, competitor_product_id: int, price: float) -> CompetitorPriceHistory:
    history_entry = CompetitorPriceHistory(
        competitor_product_id=competitor_product_id,
        price=price
    )
    db.add(history_entry)
    db.commit()
    db.refresh(history_entry)
    return history_entry

def create_competitor_product(
    db: Session,
    catalog_product_id: int,
    platform_name: str,
    product_name: str,
    price: float,
    product_url: Optional[str] = None,
    seller_name: Optional[str] = None,
    product_details: Optional[str] = None,
    rating: Optional[float] = None,
    review_count: Optional[int] = None,
    availability: bool = True
) -> CompetitorProduct:
    # Search for existing competitor product for this catalog product on this platform
    db_comp = db.query(CompetitorProduct).filter(
        CompetitorProduct.catalog_product_id == catalog_product_id,
        CompetitorProduct.platform_name == platform_name
    ).first()

    if db_comp:
        # Move previous price into competitor_price_history before updating
        if db_comp.price != price:
            save_price_history(db, db_comp.id, db_comp.price)
            db_comp.price = price
            
        # Update other competitor details
        db_comp.product_name = product_name
        db_comp.product_url = product_url
        db_comp.seller_name = seller_name
        db_comp.product_details = product_details
        db_comp.rating = rating
        db_comp.review_count = review_count
        db_comp.availability = availability
        db_comp.scraped_at = datetime.now(timezone.utc)
        
        db.commit()
        db.refresh(db_comp)
    else:
        # Store new competitor product
        db_comp = CompetitorProduct(
            catalog_product_id=catalog_product_id,
            platform_name=platform_name,
            product_name=product_name,
            product_url=product_url,
            seller_name=seller_name,
            product_details=product_details,
            price=price,
            rating=rating,
            review_count=review_count,
            availability=availability
        )
        db.add(db_comp)
        db.commit()
        db.refresh(db_comp)
        
    return db_comp

def update_competitor_price(db: Session, competitor_product_id: int, new_price: float) -> CompetitorProduct:
    db_comp = db.query(CompetitorProduct).filter(CompetitorProduct.id == competitor_product_id).first()
    if db_comp and db_comp.price != new_price:
        save_price_history(db, db_comp.id, db_comp.price)
        db_comp.price = new_price
        db.commit()
        db.refresh(db_comp)
    
    return db_comp

def get_latest_competitor_prices(db: Session, catalog_product_id: int) -> List[CompetitorProduct]:
    return db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == catalog_product_id).all()

def get_all_competitor_products(db: Session) -> List[CompetitorProduct]:
    return db.query(CompetitorProduct).all()

def get_competitor_product_by_id(db: Session, competitor_id: int) -> Optional[CompetitorProduct]:
    return db.query(CompetitorProduct).filter(CompetitorProduct.id == competitor_id).first()

def get_competitor_price_history(db: Session, competitor_product_id: int) -> List[CompetitorPriceHistory]:
    return db.query(CompetitorPriceHistory).filter(CompetitorPriceHistory.competitor_product_id == competitor_product_id).order_by(CompetitorPriceHistory.recorded_at.desc()).all()
