from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException

from app.config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES
)


# Password hashing

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)


# OAuth2 Authentication

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login"
)



def get_password_hash(password: str):

    return pwd_context.hash(password)



def verify_password(
        plain_password: str,
        hashed_password: str
):

    try:
        return pwd_context.verify(
            plain_password,
            hashed_password
        )

    except Exception:
        return False



def create_access_token(
        data: dict,
        expires_delta: Optional[timedelta] = None
):

    to_encode = data.copy()


    if expires_delta:

        expire = datetime.now(timezone.utc) + expires_delta

    else:

        expire = datetime.now(timezone.utc) + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )


    to_encode.update({
        "exp": expire
    })


    return jwt.encode(
        to_encode,
        SECRET_KEY,
        algorithm=ALGORITHM
    )


def decode_access_token(token: str):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Invalid authentication token",
        headers={"WWW-Authenticate": "Bearer"}
    )
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        return user_id
    except JWTError:
        raise credentials_exception