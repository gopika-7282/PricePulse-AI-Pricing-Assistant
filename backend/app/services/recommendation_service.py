from sqlalchemy.orm import Session
from app.models.recommendation import Recommendation
from app.models.retailer_product import RetailerProduct
from typing import List, Optional

def get_all_recommendations_for_user(db: Session, user_id: int) -> List[Recommendation]:
    rows = db.query(Recommendation).join(
        RetailerProduct, Recommendation.retailer_product_id == RetailerProduct.id
    ).filter(RetailerProduct.user_id == user_id).order_by(Recommendation.id.desc()).all()
    latest_by_product = {}
    for recommendation in rows:
        latest_by_product.setdefault(recommendation.retailer_product_id, recommendation)
    return list(latest_by_product.values())

def get_recommendation_by_id(db: Session, recommendation_id: int, user_id: int) -> Optional[Recommendation]:
    return db.query(Recommendation).join(
        RetailerProduct, Recommendation.retailer_product_id == RetailerProduct.id
    ).filter(
        Recommendation.id == recommendation_id, 
        RetailerProduct.user_id == user_id
    ).first()
