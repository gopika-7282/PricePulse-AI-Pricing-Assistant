from pydantic import BaseModel, ConfigDict
from datetime import datetime

class CompetitorPriceHistoryCreate(BaseModel):
    competitor_product_id: int
    price: float

class CompetitorPriceHistoryResponse(BaseModel):
    id: int
    competitor_product_id: int
    price: float
    recorded_at: datetime

    model_config = ConfigDict(from_attributes=True)
