from pydantic import BaseModel, ConfigDict, model_validator
from datetime import datetime

class ProductUpdateRequest(BaseModel):
    product_name: str
    category: str
    brand: str
    product_details: str
    cost_price: float
    stock_quantity: int
    quantity_value: float | None = None
    quantity_unit: str | None = None
    minimum_profit_margin: float

    @model_validator(mode="after")
    def validate_pack_quantity(self):
        if (self.quantity_value is None) != (self.quantity_unit is None):
            raise ValueError("Product quantity and unit must be provided together.")
        if self.quantity_unit is not None and self.quantity_unit.casefold() not in {"g", "kg", "mg", "ml", "l"}:
            raise ValueError("Product quantity unit must be g, kg, mg, ml, or L.")
        if self.quantity_value is not None and self.quantity_value <= 0:
            raise ValueError("Product quantity must be greater than zero.")
        return self


class ProductCreateRequest(ProductUpdateRequest):
    pass


class RetailerProductCreate(BaseModel):
    catalog_product_id: int
    cost_price: float
    stock_quantity: int
    quantity_value: float | None = None
    quantity_unit: str | None = None
    minimum_profit_margin: float

class RetailerProductResponse(BaseModel):
    id: int
    catalog_product_id: int
    cost_price: float
    stock_quantity: int
    quantity_value: float | None = None
    quantity_unit: str | None = None
    minimum_profit_margin: float
    product_name: str = ""
    category: str | None = None
    brand: str | None = None
    product_details: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
