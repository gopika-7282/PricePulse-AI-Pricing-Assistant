from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime
from pydantic import field_validator

class CompetitorProductCreate(BaseModel):
    catalog_product_id: int
    platform_name: str
    product_name: str
    product_url: Optional[str] = None
    product_details: Optional[str] = None
    price: float
    rating: Optional[float] = None
    availability: Optional[bool] = None
class CompetitorProductResponse(BaseModel):
    id: int
    catalog_product_id: int
    platform_name: str
    product_name: str
    price: float
    rating: Optional[float] = None
    availability: Optional[bool] = None
    scraped_at: Optional[datetime] = None
    observation_source: str

    model_config = ConfigDict(from_attributes=True)
