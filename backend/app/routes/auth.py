from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.schemas.auth import (
    UserRegisterRequest,
    TokenResponse,
    UserResponse
)
from app.schemas.password_reset import ForgotPasswordRequest, ResetPasswordRequest
from app.services.auth_service import create_user, authenticate_user, generate_access_token_for_user, get_current_user, get_db
from app.services.password_reset_service import process_forgot_password, process_reset_password
from app.models.user import User

router = APIRouter()

@router.post("/register", response_model=UserResponse, status_code=201)
def register(request: UserRegisterRequest, db: Session = Depends(get_db)):
    user = create_user(db, request)
    return user

@router.post("/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2 sends username field
    # We use email as username
    user = authenticate_user(db, form_data.username, form_data.password)
    
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
        
    access_token = generate_access_token_for_user(user.id)
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/forgot-password")
def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    return process_forgot_password(db, request.email)

@router.post("/reset-password")
def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    if not process_reset_password(db, request.token, request.new_password):
        raise HTTPException(status_code=400, detail="This password-reset link is invalid or has expired. Request a new one.")
    return {"message": "Password reset successful. Please sign in with your new password."}

@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user

@router.post("/logout")
def logout(current_user: User = Depends(get_current_user)):
    # Since JWT is stateless, logout must occur on the client by deleting the token.
    # Optionally, a token blacklist could be implemented if strict server-side invalidation is needed.
    return {"message": "Successfully logged out. Please remove the token from your client."}
