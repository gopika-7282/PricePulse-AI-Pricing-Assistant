from fastapi import FastAPI
from app.database import engine, Base
import app.models
import logging
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from app.utils.auth_validation import auth_validation_error_payload

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


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled request failure for %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "The request could not be completed. Please try again."})


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    safe_payload = auth_validation_error_payload(request.url.path)
    if safe_payload is not None:
        # Pydantic's default validation payload includes the rejected input,
        # which could disclose a password. Keep auth validation responses value-free.
        return JSONResponse(status_code=422, content=safe_payload)
    return await request_validation_exception_handler(request, exc)


# Add CORS last so it wraps the request logger and handled failures as well as
# normal responses. Credentialed JWT requests stay restricted to the Vite UI.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
