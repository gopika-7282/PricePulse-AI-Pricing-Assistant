from fastapi import APIRouter

from app.routes.product import router as product_router
from app.routes.competitor import router as competitor_router
from app.routes.pricing import router as pricing_router
from app.routes.recommendation import router as recommendation_router
from app.routes.agent_log import router as agent_log_router
from app.routes.catalog import router as catalog_router
from app.routes.workflow import router as workflow_router

router = APIRouter()

router.include_router(product_router, prefix="/products", tags=["Products"])
router.include_router(catalog_router, prefix="/catalog", tags=["Catalog"])
router.include_router(competitor_router, tags=["Competitors"])
router.include_router(pricing_router, prefix="/pricing", tags=["Pricing Analysis"])
router.include_router(recommendation_router, prefix="/recommendations", tags=["Recommendations"])
router.include_router(agent_log_router, prefix="/agent-logs", tags=["Agent Logs"])
router.include_router(workflow_router, tags=["Agentic Pricing Workflow"])
