"""SMTP delivery for account emails."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlencode

from app import config

logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    """Raised when a configured SMTP server cannot deliver a message."""


def build_password_reset_url(token: str) -> str:
    base_url = config.FRONTEND_BASE_URL.rstrip("/")
    return f"{base_url}/reset-password?{urlencode({'token': token})}"


def send_password_reset_email(recipient: str, token: str) -> None:
    """Send a short-lived reset link without logging message contents."""
    message = EmailMessage()
    message["Subject"] = "Reset your PricePulse password"
    message["From"] = config.SMTP_FROM_EMAIL
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your PricePulse password.\n\n"
        f"Use this link within 15 minutes: {build_password_reset_url(token)}\n\n"
        "If you did not request this, you can ignore this email."
    )

    try:
        if config.SMTP_USE_SSL:
            connection = smtplib.SMTP_SSL(
                config.SMTP_HOST,
                config.SMTP_PORT,
                timeout=10,
                context=ssl.create_default_context(),
            )
        else:
            connection = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10)

        with connection as smtp:
            if not config.SMTP_USE_SSL and config.SMTP_USE_TLS:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)
    except Exception as exc:
        logger.warning("Password-reset email delivery failed")
        raise EmailDeliveryError("SMTP delivery failed") from exc

    logger.info("Password-reset email sent successfully")
