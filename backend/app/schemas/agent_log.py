from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class AgentLogResponse(BaseModel):
    id: int
    retailer_product_id: int
    agent_name: str
    task_description: str
    input_data: Optional[str] = None
    output_data: Optional[str] = None
    execution_time: Optional[float] = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
