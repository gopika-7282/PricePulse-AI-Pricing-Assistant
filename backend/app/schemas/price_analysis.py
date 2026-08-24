from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class PriceAnalysisResponse(BaseModel):
    id: int
    retailer_product_id: int
    average_market_price: Optional[float] = None
    minimum_market_price: Optional[float] = None
    maximum_market_price: Optional[float] = None
    competitor_count: int = 0
    price_difference_percentage: Optional[float] = None
    analysis_summary: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
