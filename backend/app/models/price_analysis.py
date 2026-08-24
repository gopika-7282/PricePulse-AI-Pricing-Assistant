from sqlalchemy import Column, Integer, Float, Text, ForeignKey, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base

class PriceAnalysis(Base):
    __tablename__ = "price_analysis"

    id = Column(Integer, primary_key=True, index=True)
    retailer_product_id = Column(Integer, ForeignKey("retailer_products.id", ondelete="CASCADE"), nullable=False, index=True)
    average_market_price = Column(Float, nullable=True)
    minimum_market_price = Column(Float, nullable=True)
    maximum_market_price = Column(Float, nullable=True)
    competitor_count = Column(Integer, default=0)
    price_difference_percentage = Column(Float, nullable=True)
    analysis_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    retailer_product = relationship("RetailerProduct", back_populates="price_analysis")
