from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.services.auth_service import get_current_user, get_db
from app.models.user import User

from app.services.product_service import (
    create_retailer_product, get_retailer_products, get_retailer_product_by_id,
    update_retailer_product, delete_retailer_product
)
from app.schemas.retailer_product import RetailerProductResponse, ProductCreateRequest, ProductUpdateRequest

router = APIRouter()

@router.post("", response_model=RetailerProductResponse, status_code=201)
def create_product(
    request: ProductCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        return create_retailer_product(
            db=db,
            user_id=current_user.id,
            name=request.product_name,
            category=request.category,
            brand=request.brand,
            product_details=request.product_details,
            cost_price=request.cost_price,
            stock_quantity=request.stock_quantity,
            minimum_profit_margin=request.minimum_profit_margin
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("", response_model=List[RetailerProductResponse])
def get_products(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_retailer_products(db, current_user.id)

@router.get("/{product_id}", response_model=RetailerProductResponse)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    product = get_retailer_product_by_id(db, product_id, current_user.id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product

@router.put("/{product_id}", response_model=RetailerProductResponse)
def update_product(
    product_id: int,
    request: ProductUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    product = update_retailer_product(
        db=db,
        product_id=product_id,
        user_id=current_user.id,
        cost_price=request.cost_price,
        stock_quantity=request.stock_quantity,
        minimum_profit_margin=request.minimum_profit_margin
    )
    if not product:
        raise HTTPException(status_code=403, detail="Not allowed or product not found")
    return product

@router.delete("/{product_id}", status_code=204)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    success = delete_retailer_product(db, product_id, current_user.id)
    if not success:
        raise HTTPException(status_code=403, detail="Not allowed or product not found")
    return None
