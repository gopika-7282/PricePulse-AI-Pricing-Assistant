from pathlib import Path
from dotenv import load_dotenv
import os

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
load_dotenv()


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
OLLAMA_TIMEOUT  = float(os.getenv("OLLAMA_TIMEOUT", "30.0"))
