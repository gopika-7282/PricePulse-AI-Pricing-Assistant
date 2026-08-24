from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from datetime import datetime

class ProductCatalogCreate(BaseModel):
    product_name: str
    category: Optional[str] = None
    brand: Optional[str] = None
    product_details: Optional[str] = None

class ProductCatalogResponse(BaseModel):
    id: int
    product_name: str = Field(validation_alias="name")
    category: Optional[str] = None
    brand: Optional[str] = None
    product_details: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
