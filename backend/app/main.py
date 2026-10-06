from fastapi import FastAPI
from app.database import engine, Base
import app.models
import logging

from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

app = FastAPI(
    title="PricePulse API",
    description="AI-powered pricing assistant backend",
    version="1.0.0"
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Log method and path only — never log Authorization headers or tokens
    logger.debug(f"REQ: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.debug(f"RSP: {request.method} {request.url.path} status={response.status_code}")
    return response

from app.routes.auth import router as auth_router
from app.routes.api import router as api_router

app.include_router(auth_router, prefix="/auth", tags=["Authentication"])
app.include_router(api_router, prefix="/api", tags=["api"])

@app.get("/")
def home():
    return {
        "message": "Welcome to PricePulse API",
        "status": "Running"
    }