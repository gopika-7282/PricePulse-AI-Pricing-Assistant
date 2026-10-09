from pathlib import Path
from dotenv import load_dotenv
import os
import math

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    # Load one deterministic project-local file; process environment values
    # still take precedence for deployment and restart configuration.
    load_dotenv(dotenv_path=env_path, override=False)


# Database
DATABASE_URL = os.getenv("DATABASE_URL")

# Authentication
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))

# Scraping
SCRAPE_FRESHNESS_THRESHOLD_DAYS = int(os.getenv("SCRAPE_FRESHNESS_THRESHOLD_DAYS", "7"))
# Maximum products fetched per platform (hard cap enforced in each scraper: max=10)
MAX_DETAIL_PRODUCTS = int(os.getenv("MAX_DETAIL_PRODUCTS", "10"))
PLAYWRIGHT_TIMEOUT = int(os.getenv("PLAYWRIGHT_TIMEOUT", "30000"))
SCRAPE_DELAY_MS = int(os.getenv("SCRAPE_DELAY_MS", "1500"))
SCRAPER_DEBUG = os.getenv("SCRAPER_DEBUG", "false").lower() == "true"
# Platform enable flags: set to "true" in .env to enable each platform
SCRAPER_FLIPKART_ENABLED = os.getenv("SCRAPER_FLIPKART_ENABLED", "true").lower() == "true"
SCRAPER_AMAZON_ENABLED = os.getenv("SCRAPER_AMAZON_ENABLED", "true").lower() == "true"
SCRAPER_MYNTRA_ENABLED = os.getenv("SCRAPER_MYNTRA_ENABLED", "true").lower() == "true"
SCRAPER_MEESHO_ENABLED = os.getenv("SCRAPER_MEESHO_ENABLED", "true").lower() == "true"

# AI Matching - PostgreSQL candidates + Qwen3 identity resolution
MATCH_CONFIG = {
    "top_k_candidates": 5,
}

# Qwen3 / Ollama configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "qwen3:8b")
def _ollama_timeout(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number of seconds") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


OLLAMA_TIMEOUT = _ollama_timeout("OLLAMA_TIMEOUT", 180)
OLLAMA_IDENTITY_TIMEOUT = _ollama_timeout("OLLAMA_IDENTITY_TIMEOUT", 180)
OLLAMA_RELEVANCE_TIMEOUT = _ollama_timeout("OLLAMA_RELEVANCE_TIMEOUT", 45)
OLLAMA_STRATEGIST_TIMEOUT = _ollama_timeout("OLLAMA_STRATEGIST_TIMEOUT", 180)

# Optional password-reset email delivery. When absent, reset requests remain
# generic and no reset secret is exposed through the API.
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "false" if SMTP_USE_SSL else "true").lower() == "true"
SMTP_PORT = int(os.getenv("SMTP_PORT", "465" if SMTP_USE_SSL else "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", os.getenv("FRONTEND_URL", "http://localhost:5173"))
# SMTP delivery is enabled only when every required credential/address is set.
EMAIL_DELIVERY_CONFIGURED = all(
    value is not None and bool(value.strip())
    for value in (SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL)
)
# Compatibility alias for clients/tests using the earlier setting name.
FRONTEND_URL = FRONTEND_BASE_URL
