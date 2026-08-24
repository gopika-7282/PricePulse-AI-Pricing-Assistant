from sqlalchemy import Column, Integer, Float, Text, Boolean, ForeignKey, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base

class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, index=True)
    retailer_product_id = Column(Integer, ForeignKey("retailer_products.id", ondelete="CASCADE"), nullable=False, index=True)
    recommended_price = Column(Float, nullable=False)
    expected_profit = Column(Float, nullable=False)
    profit_percentage = Column(Float, nullable=False)
    reasoning = Column(Text, nullable=False)
    confidence_score = Column(Float, nullable=True)
    accepted_by_user = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    retailer_product = relationship("RetailerProduct", back_populates="recommendation")
