from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.services.auth_service import get_db, get_current_user
from app.models.user import User
from app.services.product_service import create_product_catalog, get_catalog_product_by_id
from app.schemas.product_catalog import ProductCatalogCreate, ProductCatalogResponse

router = APIRouter()

@router.post("", response_model=ProductCatalogResponse, status_code=201)
def create_catalog(
    request: ProductCatalogCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return create_product_catalog(
        db=db,
        name=request.product_name,
        category=request.category,
        brand=request.brand,
        product_details=request.product_details
    )

@router.get("", response_model=List[ProductCatalogResponse])
def get_catalog(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Catalog entries are global generic product identities. Return only those
    # referenced by the current user's retailer products.
    from app.models.retailer_product import RetailerProduct
    from app.models.product_catalog import ProductCatalog
    return db.query(ProductCatalog).join(RetailerProduct).filter(RetailerProduct.user_id == current_user.id).distinct().all()

@router.get("/{id}", response_model=ProductCatalogResponse)
def get_catalog_item(
    id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from app.models.retailer_product import RetailerProduct
    item = get_catalog_product_by_id(db, id)
    if not db.query(RetailerProduct).filter_by(user_id=current_user.id, catalog_product_id=id).first():
        item = None
    if not item:
        raise HTTPException(status_code=404, detail="Catalog product not found")
    return item
