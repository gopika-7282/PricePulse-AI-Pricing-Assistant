from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional
from pydantic import field_validator

class CompetitorPriceHistoryCreate(BaseModel):
    competitor_product_id: int
    price: float
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value

class CompetitorPriceHistoryResponse(BaseModel):
    id: int
    competitor_product_id: int
    price: float
    scraped_at: Optional[datetime] = None
    observation_source: str

    model_config = ConfigDict(from_attributes=True)
