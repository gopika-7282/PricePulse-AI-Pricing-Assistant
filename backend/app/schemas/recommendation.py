from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class RecommendationResponse(BaseModel):
    id: int
    retailer_product_id: int
    recommended_price: float
    expected_profit: float
    profit_percentage: float
    reasoning: str
    confidence_score: Optional[float] = None
    accepted_by_user: Optional[bool] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
