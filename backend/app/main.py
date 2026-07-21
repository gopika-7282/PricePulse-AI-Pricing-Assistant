from fastapi import FastAPI


app = FastAPI(
    title="PricePulse API",
    description="AI-powered pricing assistant backend",
    version="1.0.0"
)


@app.get("/")
def home():
    return {
        "message": "Welcome to PricePulse API",
        "status": "Running"
    }