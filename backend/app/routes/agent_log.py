from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List

from app.services.auth_service import get_db, get_current_user
from app.models.user import User
from app.services.agent_service import get_agent_logs_for_user
from app.schemas.agent_log import AgentLogResponse

router = APIRouter()

@router.get("", response_model=List[AgentLogResponse])
def get_agent_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return get_agent_logs_for_user(db, current_user.id)
