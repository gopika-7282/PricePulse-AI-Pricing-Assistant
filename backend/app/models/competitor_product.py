from sqlalchemy import Column, Integer, String, Float, Text, Boolean, ForeignKey, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from app.database import Base

class CompetitorProduct(Base):
    __tablename__ = "competitor_products"

    id = Column(Integer, primary_key=True, index=True)
    catalog_product_id = Column(Integer, ForeignKey("product_catalog.id", ondelete="CASCADE"), nullable=False, index=True)
    platform_name = Column(String, nullable=False, index=True)
    product_name = Column(String, nullable=False)
    product_url = Column(String, nullable=True)
    seller_name = Column(String, nullable=True)
    product_details = Column(Text, nullable=True)
    price = Column(Float, nullable=False)
    rating = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    availability = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    catalog_product = relationship("ProductCatalog", back_populates="competitor_products")
    price_history = relationship("CompetitorPriceHistory", back_populates="competitor_product", cascade="all, delete-orphan")
