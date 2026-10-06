from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class CompetitorProductCreate(BaseModel):
    catalog_product_id: int
    platform_name: str
    product_name: str
    product_url: Optional[str] = None
    product_details: Optional[str] = None
    price: float
    rating: Optional[float] = None
    availability: bool = True

class CompetitorProductResponse(BaseModel):
    id: int
    catalog_product_id: int
    platform_name: str
    product_name: str
    price: float
    rating: Optional[float] = None
    availability: bool
    scraped_at: datetime

    model_config = ConfigDict(from_attributes=True)
