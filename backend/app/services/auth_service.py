from sqlalchemy.orm import Session
from fastapi import HTTPException, Depends, status
from jose import jwt, JWTError

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


    existing_user=db.query(User).filter(
        User.email==user.email
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
        email=user.email,

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


    user=db.query(User).filter(
        User.email==email
    ).first()



    if not user:

        return None



    if not verify_password(
        password,
        user.password_hash
    ):

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