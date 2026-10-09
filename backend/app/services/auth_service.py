from sqlalchemy.orm import Session
from fastapi import HTTPException, Depends, status
from jose import jwt, JWTError
import logging

from app.models.user import User
from app.schemas.auth import UserRegisterRequest

from app.database import SessionLocal

from app.utils.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    oauth2_scheme,
    decode_access_token
)
from sqlalchemy.exc import SQLAlchemyError

from app.config import SECRET_KEY, ALGORITHM

logger = logging.getLogger(__name__)



# ======================
# Database Dependency
# ======================

def get_db():

    db=SessionLocal()

    try:
        yield db

    finally:
        db.close()



# ======================
# Create User
# ======================

def create_user(
        db:Session,
        user:UserRegisterRequest
):

    # Normalize email to prevent case/whitespace mismatch
    normalized_email = user.email.strip().lower()

    existing_user=db.query(User).filter(
        User.email==normalized_email
    ).first()


    if existing_user:

        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )



    hashed_password=get_password_hash(
        user.password
    )


    new_user = User(
        retailer_name=user.retailer_name,
        email=normalized_email,

        password_hash=hashed_password,

        business_name=user.business_name

    )


    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error occurred")


    return new_user




# ======================
# Login Verification
# ======================

def authenticate_user(
        db:Session,
        email:str,
        password:str
):

    # Normalize email to match registration
    normalized_email = email.strip().lower()
    # Order by id desc to handle any old duplicate records
    user=db.query(User).filter(
        User.email==normalized_email
    ).order_by(User.id.desc()).first()
    
    user_exists = user is not None
    logger.info(f"User exists: {user_exists}")

    if not user:
        return None

    # Verify if hash format is valid bcrypt (starts with $2b$ or $2a$)
    hash_valid = user.password_hash.startswith('$2') if user.password_hash else False
    logger.info(f"Hash format valid: {hash_valid}")

    password_valid = verify_password(
        password,
        user.password_hash
    )
    logger.info(f"Password verification result: {password_valid}")

    if not password_valid:
        return None

    return user




# ======================
# Generate JWT
# ======================


def generate_access_token_for_user(
        user_id:int
):


    return create_access_token(
        {
            "sub":str(user_id)
        }
    )




# ======================
# Current User
# ======================


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
):

    user_id = decode_access_token(token)

    user = db.query(User).filter(
        User.id == int(user_id)
    ).first()

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return user
