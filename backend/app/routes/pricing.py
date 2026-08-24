from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.services.auth_service import get_db, get_current_user
from app.models.user import User

from app.services.pricing_service import calculate_price_analysis, get_price_analysis
from app.schemas.price_analysis import PriceAnalysisResponse
from app.services.product_service import get_retailer_product_by_id

router = APIRouter()

class PriceAnalyzeRequest(BaseModel):
    retailer_product_id: int

@router.post("/analyze", response_model=PriceAnalysisResponse, status_code=201)
def analyze_pricing(
    request: PriceAnalyzeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Enforce ownership on analysis target!
    product = get_retailer_product_by_id(db, request.retailer_product_id, current_user.id)
    if not product:
        raise HTTPException(status_code=403, detail="Product ownership validation failed.")

    try:
        return calculate_price_analysis(db, request.retailer_product_id)
    except ValueError as val_err:
        raise HTTPException(status_code=404, detail=str(val_err))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to compute price analysis")

@router.get("/{product_id}", response_model=PriceAnalysisResponse)
def get_pricing_analysis(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    analysis = get_price_analysis(db, product_id, current_user.id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return analysis
