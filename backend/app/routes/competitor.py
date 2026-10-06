from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.services.auth_service import get_db, get_current_user
from app.models.user import User
from app.models.retailer_product import RetailerProduct
from app.models.competitor_product import CompetitorProduct

from app.services.competitor_service import (
    upsert_competitor_product, get_all_competitor_products,
    get_competitor_product_by_id, save_price_history, get_competitor_price_history
)
from app.schemas.competitor_product import CompetitorProductCreate, CompetitorProductResponse
from app.schemas.competitor_price_history import CompetitorPriceHistoryCreate, CompetitorPriceHistoryResponse

router = APIRouter()

@router.post("/competitors", response_model=CompetitorProductResponse, status_code=201)
def create_competitor(
    request: CompetitorProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not db.query(RetailerProduct).filter_by(user_id=current_user.id, catalog_product_id=request.catalog_product_id).first():
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        return upsert_competitor_product(
            db=db,
            catalog_product_id=request.catalog_product_id,
            platform_name=request.platform_name,
            product_name=request.product_name,
            price=request.price,
            product_url=request.product_url,
            product_details=request.product_details,
            rating=request.rating,
            availability=request.availability
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid competitor data provided")

@router.get("/competitors", response_model=List[CompetitorProductResponse])
def get_competitors(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owned_catalog_ids = db.query(RetailerProduct.catalog_product_id).filter_by(user_id=current_user.id)
    return db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id.in_(owned_catalog_ids)).all()

@router.get("/competitors/{id}", response_model=CompetitorProductResponse)
def get_competitor(id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    comp = db.query(CompetitorProduct).join(RetailerProduct, RetailerProduct.catalog_product_id == CompetitorProduct.catalog_product_id).filter(CompetitorProduct.id == id, RetailerProduct.user_id == current_user.id).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Competitor not found")
    return comp

@router.post("/competitor-price-history", response_model=CompetitorPriceHistoryResponse, status_code=201)
def add_price_history(
    request: CompetitorPriceHistoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    owned = db.query(CompetitorProduct).join(RetailerProduct, RetailerProduct.catalog_product_id == CompetitorProduct.catalog_product_id).filter(CompetitorProduct.id == request.competitor_product_id, RetailerProduct.user_id == current_user.id).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Competitor not found")
    try:
        return save_price_history(db, request.competitor_product_id, request.price)
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to save price history")

@router.get("/competitor-price-history/{competitor_product_id}", response_model=List[CompetitorPriceHistoryResponse])
def get_price_history(competitor_product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owned = db.query(CompetitorProduct).join(RetailerProduct, RetailerProduct.catalog_product_id == CompetitorProduct.catalog_product_id).filter(CompetitorProduct.id == competitor_product_id, RetailerProduct.user_id == current_user.id).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Competitor not found")
    return get_competitor_price_history(db, competitor_product_id)
