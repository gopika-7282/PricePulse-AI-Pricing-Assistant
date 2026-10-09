from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.user import User
from app.routes.auth import forgot_password, reset_password
from app.schemas.password_reset import ForgotPasswordRequest, ResetPasswordRequest
from app.services.password_reset_service import RESET_CONFIRMATION, RESET_EMAIL_UNAVAILABLE, process_forgot_password, process_reset_password
from app.utils.security import get_password_hash, verify_password


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def configured_mail(monkeypatch):
    monkeypatch.setattr("app.config.EMAIL_DELIVERY_CONFIGURED", True)
    monkeypatch.setattr("app.config.SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr("app.config.SMTP_USERNAME", "mailer@example.com")
    monkeypatch.setattr("app.config.SMTP_PASSWORD", "test-only-secret")
    monkeypatch.setattr("app.config.SMTP_FROM_EMAIL", "no-reply@example.com")
    monkeypatch.setattr("app.config.SMTP_USE_SSL", False)
    monkeypatch.setattr("app.config.SMTP_USE_TLS", True)


def create_user(db):
    user = User(email="reset@example.com", retailer_name="Retailer", password_hash=get_password_hash("OldPass1!"))
    db.add(user)
    db.commit()
    return user


def test_forgot_password_is_generic_for_existing_and_unknown_emails(db, configured_mail):
    create_user(db)
    with patch("app.services.password_reset_service._send_password_reset_email", return_value=True):
        known = forgot_password(ForgotPasswordRequest(email="reset@example.com"), db)
        unknown = forgot_password(ForgotPasswordRequest(email="nobody@example.com"), db)
    assert known == unknown == {"message": RESET_CONFIRMATION, "email_delivery_configured": True}
    assert "token" not in known


def test_reset_email_is_not_sent_or_token_exposed_without_mail_configuration(monkeypatch, db, caplog):
    user = create_user(db)
    monkeypatch.setattr("app.config.SMTP_HOST", None)
    monkeypatch.setattr("app.config.EMAIL_DELIVERY_CONFIGURED", False)
    monkeypatch.setattr("app.config.SMTP_FROM_EMAIL", None)
    with patch("app.services.email_service.smtplib.SMTP_SSL") as smtp:
        result = process_forgot_password(db, "reset@example.com")
    assert result == {"message": RESET_EMAIL_UNAVAILABLE, "email_delivery_configured": False}
    assert "token" not in result
    assert "reset@example.com" not in caplog.text
    assert user.reset_token is None
    smtp.assert_not_called()


def test_reset_email_uses_configured_starttls_and_frontend_base_url(monkeypatch):
    from app.services.email_service import send_password_reset_email
    from email.message import EmailMessage

    monkeypatch.setattr("app.config.SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr("app.config.SMTP_PORT", 587)
    monkeypatch.setattr("app.config.SMTP_USERNAME", "mailer@example.test")
    monkeypatch.setattr("app.config.SMTP_PASSWORD", "test-only-secret")
    monkeypatch.setattr("app.config.SMTP_FROM_EMAIL", "no-reply@example.test")
    monkeypatch.setattr("app.config.SMTP_USE_SSL", False)
    monkeypatch.setattr("app.config.SMTP_USE_TLS", True)
    monkeypatch.setattr("app.config.FRONTEND_BASE_URL", "https://pricepulse.example.test/")
    with patch("app.services.email_service.smtplib.SMTP") as smtp_factory:
        smtp = smtp_factory.return_value.__enter__.return_value
        send_password_reset_email("customer@example.test", "opaque-reset-token")
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("mailer@example.test", "test-only-secret")
    message = smtp.send_message.call_args.args[0]
    assert isinstance(message, EmailMessage)
    assert message["To"] == "customer@example.test"
    assert "https://pricepulse.example.test/reset-password?token=opaque-reset-token" in message.get_content()


def test_reset_token_is_random_hashed_delivered_and_single_use(db, configured_mail):
    user = create_user(db)
    delivered = {}
    def capture(email, token):
        delivered.update(email=email, token=token)
        return True
    with patch("app.services.password_reset_service._send_password_reset_email", side_effect=capture):
        result = process_forgot_password(db, " RESET@example.com ")
    assert result == {"message": RESET_CONFIRMATION, "email_delivery_configured": True}
    assert delivered["email"] == user.email
    assert user.reset_token != delivered["token"]
    assert len(user.reset_token) == 64
    assert process_reset_password(db, "not-the-token", "NewPass2!") is False
    assert process_reset_password(db, delivered["token"], "NewPass2!") is True
    assert user.reset_token is None and user.reset_token_expiry is None
    assert not verify_password("OldPass1!", user.password_hash)
    assert verify_password("NewPass2!", user.password_hash)
    assert process_reset_password(db, delivered["token"], "Another3!") is False


def test_expired_reset_token_is_rejected(db, configured_mail):
    user = create_user(db)
    with patch("app.services.password_reset_service._send_password_reset_email", return_value=True):
        process_forgot_password(db, user.email)
    user.reset_token_expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    with patch("app.services.password_reset_service._token_digest", return_value=user.reset_token):
        assert process_reset_password(db, "expired-token", "NewPass2!") is False
    assert verify_password("OldPass1!", user.password_hash)


def test_reset_endpoint_returns_safe_error_for_wrong_or_expired_tokens(db):
    with pytest.raises(HTTPException) as error:
        reset_password(ResetPasswordRequest(token="wrong", new_password="NewPass2!"), db)
    assert error.value.status_code == 400
    assert "invalid or has expired" in error.value.detail


def test_auth_validation_response_never_echoes_password():
    from app.utils.auth_validation import auth_validation_error_payload
    plaintext = "secret-but-invalid"
    payload = auth_validation_error_payload("/auth/reset-password")
    assert plaintext not in str(payload)
    assert "new_password" not in str(payload)
    assert auth_validation_error_payload("/api/products") is None


@pytest.mark.parametrize("password", ["short", "alllowercase1!", "NoDigits!!", "NoSymbol123"])
def test_reset_password_policy_rejects_weak_passwords(password):
    with pytest.raises(ValueError):
        ResetPasswordRequest(token="opaque", new_password=password)


def test_reset_endpoint_success_requires_sign_in_and_does_not_return_credentials(db, configured_mail):
    user = create_user(db)
    delivered = {}
    with patch("app.services.password_reset_service._send_password_reset_email", side_effect=lambda _email, token: delivered.setdefault("token", token) or True):
        process_forgot_password(db, user.email)
    response = reset_password(ResetPasswordRequest(token=delivered["token"], new_password="NewPass2!"), db)
    assert response["message"].startswith("Password reset successful")
    assert "token" not in response and "password" not in response


def test_missing_smtp_does_not_create_reset_token(monkeypatch, db):
    user = create_user(db)
    monkeypatch.setattr("app.config.EMAIL_DELIVERY_CONFIGURED", False)
    monkeypatch.setattr("app.config.SMTP_HOST", None)
    known = process_forgot_password(db, user.email)
    unknown = process_forgot_password(db, "nobody@example.com")
    assert known == unknown == {"message": RESET_EMAIL_UNAVAILABLE, "email_delivery_configured": False}
    assert user.reset_token is None


def test_missing_smtp_response_does_not_reveal_account_existence(monkeypatch, db):
    create_user(db)
    monkeypatch.setattr("app.config.EMAIL_DELIVERY_CONFIGURED", False)
    response = process_forgot_password(db, "reset@example.com")
    assert response == {"message": RESET_EMAIL_UNAVAILABLE, "email_delivery_configured": False}


def test_smtp_failure_returns_controlled_error_and_invalidates_token(db, configured_mail):
    user = create_user(db)
    from app.services.email_service import EmailDeliveryError
    with patch("app.services.password_reset_service._send_password_reset_email", side_effect=EmailDeliveryError()):
        with pytest.raises(HTTPException) as error:
            process_forgot_password(db, user.email)
    assert error.value.status_code == 503
    assert error.value.detail == "Unable to send password reset email. Please try again later."
    assert user.reset_token is None and user.reset_token_expiry is None


def test_smtp_configuration_requires_all_nonblank_credentials(monkeypatch, db):
    create_user(db)
    for field in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL"):
        monkeypatch.setattr("app.config.EMAIL_DELIVERY_CONFIGURED", False)
        monkeypatch.setattr("app.config." + field, "   ")
        with patch("app.services.password_reset_service._send_password_reset_email") as sender:
            response = process_forgot_password(db, "reset@example.com")
        assert response["email_delivery_configured"] is False
        sender.assert_not_called()
