from sqlalchemy.orm import Session
from fastapi import HTTPException
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.exc import SQLAlchemyError

from app.models.user import User
from app.utils.security import get_password_hash

def process_forgot_password(db: Session, email: str):
    user = db.query(User).filter(User.email == email).first()
    
    if not user:
        raise HTTPException(
            status_code=404,
            detail="Email not registered"
        )
        
    reset_token = secrets.token_urlsafe(32)
    reset_token_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    
    user.reset_token = reset_token
    user.reset_token_expiry = reset_token_expiry
    
    try:
        db.commit()
        db.refresh(user)
        
        # Verify persistence
        if user.reset_token is None or user.reset_token_expiry is None:
            raise SQLAlchemyError("Reset token failed to persist.")
            
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error occurred")
    
    return {"message": "Reset token generated", "token": user.reset_token}

def process_reset_password(db: Session, token: str, new_password: str):
    user = db.query(User).filter(User.reset_token == token).first()
    
    if not user:
        raise HTTPException(status_code=400, detail="Invalid token")
        
    if not user.reset_token_expiry:
        raise HTTPException(status_code=400, detail="Invalid token")
        
    expiry_time = user.reset_token_expiry
    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)

    current_time = datetime.now(timezone.utc)
    if current_time > expiry_time:
        raise HTTPException(status_code=400, detail="Token has expired")
        
    hashed_password = get_password_hash(new_password)
    user.password_hash = hashed_password
    
    # Invalidate the token completely upon success
    user.reset_token = None
    user.reset_token_expiry = None
    
    try:
        db.commit()
        db.refresh(user)
        
        # Verify invalidation
        if user.reset_token is not None:
             raise SQLAlchemyError("Failed to clear reset token")
             
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Database error occurred"
        )
    return {"message": "Password updated successfully"}