from sqlalchemy import Column, Integer, Float, ForeignKey, DateTime, String, Text, Boolean, text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base

class RetailerProduct(Base):
    __tablename__ = "retailer_products"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    catalog_product_id = Column(Integer, ForeignKey("product_catalog.id", ondelete="CASCADE"), nullable=False, index=True)
    cost_price = Column(Float, nullable=False)
    stock_quantity = Column(Integer, nullable=False, default=0)
    quantity_value = Column(Float, nullable=True)
    quantity_unit = Column(String(16), nullable=True)
    minimum_profit_margin = Column(Float, nullable=False)
    name_override = Column(String, nullable=True)
    category_override = Column(String, nullable=True)
    brand_override = Column(String, nullable=True)
    product_details_override = Column(Text, nullable=True)
    catalog_identity_pending = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    catalog_identity_staging = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="retailer_products")
    catalog_product = relationship("ProductCatalog", back_populates="retailer_products")
    
    price_analysis = relationship("PriceAnalysis", back_populates="retailer_product", cascade="all, delete-orphan")
    recommendation = relationship("Recommendation", back_populates="retailer_product", cascade="all, delete-orphan")
    agent_logs = relationship("AgentLog", back_populates="retailer_product", cascade="all, delete-orphan")

    @property
    def product_name(self):
        return self.name_override if self.name_override is not None else (self.catalog_product.name if self.catalog_product else "")

    @property
    def category(self):
        return self.category_override if self.category_override is not None else (self.catalog_product.category if self.catalog_product else None)

    @property
    def brand(self):
        return self.brand_override if self.brand_override is not None else (self.catalog_product.brand if self.catalog_product else None)

    @property
    def product_details(self):
        return self.product_details_override if self.product_details_override is not None else (self.catalog_product.product_details if self.catalog_product else None)
