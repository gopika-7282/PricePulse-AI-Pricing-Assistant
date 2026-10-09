from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    id = Column(Integer, primary_key=True, index=True)
    retailer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("retailer_products.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan", order_by="ChatMessage.id")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    conversation = relationship("ChatConversation", back_populates="messages")
