from sqlalchemy.orm import Session
from app.models.agent_log import AgentLog
from app.models.retailer_product import RetailerProduct
from typing import List

def get_agent_logs_for_user(db: Session, user_id: int) -> List[AgentLog]:
    return db.query(AgentLog).join(
        RetailerProduct, AgentLog.retailer_product_id == RetailerProduct.id
    ).filter(RetailerProduct.user_id == user_id).order_by(AgentLog.created_at.desc()).all()
