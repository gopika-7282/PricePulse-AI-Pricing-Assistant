from pydantic import BaseModel, EmailStr, field_validator, ConfigDict
from typing import Optional
from datetime import datetime
import re


# ==========================
# Register Schema
# ==========================

class UserRegisterRequest(BaseModel):

    retailer_name: str
    email: EmailStr
    password: str
    business_name: Optional[str] = None


    @field_validator("password")
    @classmethod
    def validate_password(cls, value):

        if len(value) < 8:
            raise ValueError(
                "Password must contain minimum 8 characters"
            )

        if not re.search(r"[A-Z]", value):
            raise ValueError(
                "Password must contain at least one uppercase letter"
            )

        if not re.search(r"[a-z]", value):
            raise ValueError(
                "Password must contain at least one lowercase letter"
            )

        if not re.search(r"[0-9]", value):
            raise ValueError(
                "Password must contain at least one number"
            )

        if not re.search(r"[@#$%!*!?&]", value):
            raise ValueError(
                "Password must contain at least one special character"
            )

        return value



# ==========================
# Login Schema
# ==========================

# Used for normal frontend/API requests
# Swagger uses OAuth2PasswordRequestForm

class LoginRequest(BaseModel):

    email: EmailStr
    password: str



# ==========================
# JWT Response
# ==========================

class TokenResponse(BaseModel):

    access_token: str
    token_type: str = "bearer"



# ==========================
# User Response
# ==========================

class UserResponse(BaseModel):

    id: int
    retailer_name: str
    email: EmailStr
    business_name: Optional[str]
    created_at: datetime
    updated_at: datetime


    model_config = ConfigDict(
        from_attributes=True
    )