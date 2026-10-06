from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.retailer_product import RetailerProduct
from app.models.recommendation import Recommendation
from app.models.user import User
from app.services.auth_service import get_current_user, get_db
from app.services.ai.chroma_service import retrieve_chatbot_context
from app.services.llm_service import generate_structured
from app.services.pricing_workflow import analyze_retailer_product

router = APIRouter()


class ChatRequest(BaseModel):
    retailer_product_id: int
    question: str = Field(min_length=2, max_length=1200)


@router.post("/workflow/{retailer_product_id}/analyze")
def analyze(retailer_product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owned = db.query(RetailerProduct).filter_by(id=retailer_product_id, user_id=current_user.id).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        result = analyze_retailer_product(db, retailer_product_id)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Price analysis could not be completed. Please try again.")
    recommendation = result.get("recommendation")
    marketplace_products = {name: [] for name in ("Amazon", "Flipkart", "Myntra", "Meesho")}
    for item in result.get("competitors", []):
        marketplace_products.setdefault(item.platform_name, []).append({
            "product_name": item.product_name,
            "price": item.price,
            "product_url": item.product_url,
            "product_details": item.product_details,
        })
    return {
        "identity": result.get("identity"),
        "marketplace_statuses": result.get("marketplace_statuses", {}),
        "marketplace_products": marketplace_products,
        "compliance": result.get("compliance", {"accepted": False, "reasons": [result.get("error", "Workflow stopped.")]}),
        "workflow": result.get("workflow", []),
        "warnings": (result.get("strategy") or {}).get("warnings", []),
        "competitor_summary": (result.get("strategy") or {}).get("competitor_summary"),
        "recommendation": ({"id": recommendation.id, "recommended_price": recommendation.recommended_price,
            "expected_profit": recommendation.expected_profit, "profit_percentage": recommendation.profit_percentage,
            "reasoning_summary": recommendation.reasoning, "confidence": recommendation.confidence_score}
            if recommendation else None),
    }


@router.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    product = db.query(RetailerProduct).filter_by(id=request.retailer_product_id, user_id=current_user.id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    recommendation = db.query(Recommendation).filter_by(retailer_product_id=product.id).order_by(Recommendation.id.desc()).first()
    context = retrieve_chatbot_context(request.question, product.catalog_product_id, n_results=5, retailer_product_id=product.id, user_id=current_user.id)
    if recommendation:
        context += f"\nCurrent retailer recommendation: Rs.{recommendation.recommended_price:.2f}; margin {recommendation.profit_percentage:.1f}%; summary: {recommendation.reasoning}"
    prompt = (
        "Answer concisely using only the provided product context. If the evidence is missing, say so. "
        "Do not reveal hidden reasoning or invent marketplace prices.\nQUESTION: " + request.question + "\nCONTEXT:\n" + (context or "No retrieved context.")
    )
    response = generate_structured(prompt, model="qwen3:8b")
    answer = (response.get("data") or {}).get("answer") if response.get("success") else None
    if not answer:
        answer = "I couldn't get a grounded answer right now. Please try again after the pricing analysis is available."
    return {"answer": answer, "grounded": bool(context)}
