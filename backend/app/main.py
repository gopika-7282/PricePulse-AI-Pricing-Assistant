from fastapi import FastAPI
from app.database import engine, Base
import app.models


from fastapi import FastAPI, Request

app = FastAPI(
    title="PricePulse API",
    description="AI-powered pricing assistant backend",
    version="1.0.0"
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"DEBUG REQ: {request.method} {request.url.path}")
    print(f"DEBUG HEADERS: {request.headers.get('authorization')}")
    response = await call_next(request)
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