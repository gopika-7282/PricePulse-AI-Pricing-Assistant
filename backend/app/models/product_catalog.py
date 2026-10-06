from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, validates
from sqlalchemy import event
from app.database import Base

class ProductCatalog(Base):
    __tablename__ = "product_catalog"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, nullable=False)
    category = Column(String, index=True, nullable=True)
    brand = Column(String, index=True, nullable=True)
    product_details = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_scraped_at = Column(DateTime(timezone=True), nullable=True)
    scraping_status = Column(String, default="NEVER_SCRAPED", nullable=False)

    retailer_products = relationship("RetailerProduct", back_populates="catalog_product", cascade="all, delete-orphan")
    competitor_products = relationship("CompetitorProduct", back_populates="catalog_product", cascade="all, delete-orphan")

