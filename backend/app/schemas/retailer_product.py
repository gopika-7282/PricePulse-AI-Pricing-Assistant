from pydantic import BaseModel, ConfigDict
from datetime import datetime

class ProductCreateRequest(BaseModel):
    product_name: str
    category: str
    brand: str
    product_details: str
    cost_price: float
    stock_quantity: int
    minimum_profit_margin: float

class ProductUpdateRequest(BaseModel):
    cost_price: float
    stock_quantity: int
    minimum_profit_margin: float

class RetailerProductCreate(BaseModel):
    catalog_product_id: int
    cost_price: float
    stock_quantity: int
    minimum_profit_margin: float

class RetailerProductResponse(BaseModel):
    id: int
    catalog_product_id: int
    cost_price: float
    stock_quantity: int
    minimum_profit_margin: float
    product_name: str = ""
    category: str | None = None
    brand: str | None = None
    product_details: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
