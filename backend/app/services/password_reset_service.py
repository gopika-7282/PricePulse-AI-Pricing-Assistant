"""Short-lived, single-use password reset tokens delivered only by email."""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import config
from app.models.user import User
from app.services.email_service import EmailDeliveryError, send_password_reset_email
from app.utils.security import get_password_hash

logger = logging.getLogger(__name__)
RESET_CONFIRMATION = "If an account exists for this email, reset instructions have been sent."
RESET_EMAIL_UNAVAILABLE = (
    "Password reset email delivery is not configured yet. Please configure SMTP to receive reset instructions."
)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _send_password_reset_email(email: str, token: str) -> bool:
    send_password_reset_email(email, token)
    return True


def process_forgot_password(db: Session, email: str):
    if not config.EMAIL_DELIVERY_CONFIGURED:
        logger.warning("Password-reset email delivery is not configured")
        return {"message": RESET_EMAIL_UNAVAILABLE, "email_delivery_configured": False}

    normalized_email = email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    if user is None:
        return {"message": RESET_CONFIRMATION, "email_delivery_configured": True}

    token = secrets.token_urlsafe(32)
    user.reset_token = _token_digest(token)
    user.reset_token_expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    db.commit()

    try:
        delivered = _send_password_reset_email(user.email, token)
        if not delivered:
            raise EmailDeliveryError("SMTP delivery failed")
    except EmailDeliveryError:
        user.reset_token = None
        user.reset_token_expiry = None
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Unable to send password reset email. Please try again later.",
        )

    return {"message": RESET_CONFIRMATION, "email_delivery_configured": True}


def process_reset_password(db: Session, token: str, new_password: str):
    user = db.query(User).filter(User.reset_token == _token_digest(token)).with_for_update().first()
    if user is None or user.reset_token_expiry is None:
        return False

    expiry = user.reset_token_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expiry:
        return False

    user.password_hash = get_password_hash(new_password)
    user.reset_token = None
    user.reset_token_expiry = None
    db.commit()
    return True
