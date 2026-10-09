from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import List, Optional
from datetime import datetime

class RecommendationResponse(BaseModel):
    id: int
    retailer_product_id: int
    recommended_price: float
    recommended_price_min: Optional[float] = None
    recommended_price_max: Optional[float] = None
    expected_profit: float
    profit_percentage: float
    reasoning: str
    confidence_score: Optional[float] = None
    evidence_type: str = "NO_EVIDENCE"
    basis: str = "ESTIMATE"
    reasoning_points: List[str] = Field(default_factory=list, max_length=4)
    accepted_by_user: Optional[bool] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def classify_basis(self):
        self.basis = "MARKET_DATA" if self.evidence_type in {"LIVE", "STALE_FALLBACK"} else "ESTIMATE"
        return self
