from sqlalchemy import Column, Integer, Float, ForeignKey, DateTime
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
    minimum_profit_margin = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="retailer_products")
    catalog_product = relationship("ProductCatalog", back_populates="retailer_products")
    
    price_analysis = relationship("PriceAnalysis", back_populates="retailer_product", cascade="all, delete-orphan")
    recommendation = relationship("Recommendation", back_populates="retailer_product", cascade="all, delete-orphan")
    agent_logs = relationship("AgentLog", back_populates="retailer_product", cascade="all, delete-orphan")
