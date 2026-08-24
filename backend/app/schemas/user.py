from pydantic import BaseModel, ConfigDict, EmailStr
from typing import Optional
from datetime import datetime

class UserBase(BaseModel):
    retailer_name: str
    email: EmailStr
    business_name: Optional[str] = None

class UserCreate(UserBase):
    password: str

class UserResponse(BaseModel):
    id: int
    retailer_name: str
    email: str
    business_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
