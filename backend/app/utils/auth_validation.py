"""Safe formatting for authentication validation failures."""


def auth_validation_error_payload(path: str) -> dict | None:
    """Avoid framework error payloads echoing passwords or reset tokens."""
    if path in {"/auth/register", "/auth/forgot-password", "/auth/reset-password"}:
        return {"detail": "Please check the request and try again."}
    return None
