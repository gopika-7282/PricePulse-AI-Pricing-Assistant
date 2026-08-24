from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.services.auth_service import get_db, get_current_user
from app.models.user import User
from app.services.product_service import get_retailer_product_by_id
from app.services.recommendation_service import get_all_recommendations_for_user, get_recommendation_by_id
from app.services.pricing_service import generate_recommendation
from app.schemas.recommendation import RecommendationResponse

router = APIRouter()

class GenerateRecommendationRequest(BaseModel):
    retailer_product_id: int

@router.post("/generate", response_model=RecommendationResponse, status_code=201)
def trigger_generation(
    request: GenerateRecommendationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    product = get_retailer_product_by_id(db, request.retailer_product_id, current_user.id)
    if not product:
        raise HTTPException(status_code=403, detail="Not authorized to generate recommendations for this product")
    
    try:
        return generate_recommendation(db, request.retailer_product_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to generate AI recommendation")

@router.get("", response_model=List[RecommendationResponse])
def get_recommendations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_all_recommendations_for_user(db, current_user.id)

@router.get("/{id}", response_model=RecommendationResponse)
def get_recommendation(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    rec = get_recommendation_by_id(db, id, current_user.id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return rec
