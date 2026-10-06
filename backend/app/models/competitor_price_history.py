from sqlalchemy import Column, Integer, Float, ForeignKey, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base

class CompetitorPriceHistory(Base):
    __tablename__ = "competitor_price_history"

    id = Column(Integer, primary_key=True, index=True)
    competitor_product_id = Column(Integer, ForeignKey("competitor_products.id", ondelete="CASCADE"), nullable=False, index=True)
    price = Column(Float, nullable=False)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    competitor_product = relationship("CompetitorProduct", back_populates="price_history")
