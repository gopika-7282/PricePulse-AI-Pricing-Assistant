from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.services.auth_service import get_db, get_current_user
from app.models.user import User
from app.models.recommendation import Recommendation
from app.models.retailer_product import RetailerProduct
from app.services.product_service import get_retailer_product_by_id
from app.services.recommendation_service import get_all_recommendations_for_user, get_recommendation_by_id
from app.services.pricing_workflow import analyze_retailer_product
from app.schemas.recommendation import RecommendationResponse
from app.models.competitor_product import CompetitorProduct
from app.routes.workflow import _business_reasoning

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
        workflow = analyze_retailer_product(db, request.retailer_product_id)
        recommendation = workflow.get("recommendation")
        if not recommendation:
            reasons = (workflow.get("compliance") or {}).get("reasons") or [workflow.get("error", "Workflow could not approve a recommendation.")]
            raise HTTPException(status_code=422, detail=" ".join(reasons))
        return recommendation
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to generate AI recommendation")

@router.get("", response_model=List[RecommendationResponse])
def get_recommendations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    results = []
    for rec in get_all_recommendations_for_user(db, current_user.id):
        product = get_retailer_product_by_id(db, rec.retailer_product_id, current_user.id)
        payload = RecommendationResponse.model_validate(rec).model_dump()
        competitors = db.query(CompetitorProduct).filter_by(catalog_product_id=product.catalog_product_id).all() if product else []
        payload["reasoning_points"] = _business_reasoning(db, product, rec, competitors) if product else []
        results.append(payload)
    return results

@router.get("/{id}", response_model=RecommendationResponse)
def get_recommendation(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    rec = get_recommendation_by_id(db, id, current_user.id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    product = get_retailer_product_by_id(db, rec.retailer_product_id, current_user.id)
    payload = RecommendationResponse.model_validate(rec).model_dump()
    competitors = db.query(CompetitorProduct).filter_by(catalog_product_id=product.catalog_product_id).all() if product else []
    payload["reasoning_points"] = _business_reasoning(db, product, rec, competitors) if product else []
    return payload


@router.post("/{id}/accept")
def accept_recommendation(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    recommendation = get_recommendation_by_id(db, id, current_user.id)
    if not recommendation:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    if recommendation.accepted_by_user is True:
        return {
            "accepted": True,
            "already_accepted": True,
            "recommendation_id": recommendation.id,
            "accepted_price": recommendation.recommended_price,
            "task_status": "COMPLETED",
        }

    changed = (db.query(Recommendation)
        .filter(Recommendation.id == id)
        .filter(Recommendation.retailer_product_id.in_(db.query(RetailerProduct.id).filter(RetailerProduct.user_id == current_user.id)))
        .filter(or_(Recommendation.accepted_by_user.is_(False), Recommendation.accepted_by_user.is_(None)))
        .update({Recommendation.accepted_by_user: True}, synchronize_session=False))
    db.commit()
    db.refresh(recommendation)
    if not changed and recommendation.accepted_by_user is not True:
        raise HTTPException(status_code=409, detail="This recommendation could not be accepted. Please refresh and try again.")
    return {
        "accepted": True,
        "already_accepted": False,
        "recommendation_id": recommendation.id,
        "accepted_price": recommendation.recommended_price,
        "task_status": "COMPLETED",
    }
