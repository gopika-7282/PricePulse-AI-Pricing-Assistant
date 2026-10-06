"""LangGraph orchestration for the four PricePulse agents."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session


class PricingState(TypedDict, total=False):
    db: Session
    retailer_product_id: int
    identity: Dict[str, Any]
    catalog_id: int
    marketplace_statuses: Dict[str, Any]
    competitors: List[Any]
    rag_context: str
    strategy: Dict[str, Any]
    compliance: Dict[str, Any]
    workflow: List[Dict[str, str]]
    error: str
    recommendation: Any


def identity_node(state: PricingState) -> Dict[str, Any]:
    from app.models.retailer_product import RetailerProduct
    from app.services.product_identity_service import evaluate_product_identity

    db = state["db"]
    owned = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    if not owned:
        return {"error": "Product not found", "workflow": [{"step": "IDENTITY", "status": "FAILED"}]}
    catalog = owned.catalog_product
    identity = evaluate_product_identity(db, catalog.name, catalog.product_details or "", catalog.category or "", catalog.brand or "")
    workflow = [{"step": "IDENTITY", "status": "COMPLETED" if identity.decision.value == "MATCH" else "FAILED"}]
    if identity.decision.value != "MATCH" or identity.matched_catalog_id != catalog.id:
        return {"identity": identity.model_dump(mode="json"), "catalog_id": catalog.id, "workflow": workflow, "error": "Product identity could not be confirmed; pricing analysis was stopped."}
    return {"identity": identity.model_dump(mode="json"), "catalog_id": catalog.id, "workflow": workflow}


def route_identity(state: PricingState) -> str:
    return "stop" if state.get("error") else "catalog"


def catalog_node(state: PricingState) -> Dict[str, Any]:
    from app.services.competitor_service import has_fresh_competitor_data
    from app.config import SCRAPE_FRESHNESS_THRESHOLD_DAYS

    fresh = has_fresh_competitor_data(state["db"], state["catalog_id"], SCRAPE_FRESHNESS_THRESHOLD_DAYS)
    return {"fresh": fresh, "workflow": state.get("workflow", []) + [{"step": "CATALOG", "status": "COMPLETED"}]}


def route_freshness(state: PricingState) -> str:
    return "rag" if state.get("fresh") else "scout"


def scout_node(state: PricingState) -> Dict[str, Any]:
    from app.models.product_catalog import ProductCatalog
    from app.services.scraping_service import scrape_product

    catalog = state["db"].query(ProductCatalog).filter(ProductCatalog.id == state["catalog_id"]).first()
    try:
        scrape_product(state["db"], catalog)
    except Exception:
        state["db"].rollback()
    statuses = catalog.marketplace_statuses or {}
    statuses = {name: ("SUCCESS" if str(status).upper() == "OK" else str(status).upper()) for name, status in statuses.items()}
    return {"marketplace_statuses": statuses, "workflow": state.get("workflow", []) + [{"step": "SCOUT", "status": "COMPLETED" if any(s == "SUCCESS" for s in statuses.values()) else "FAILED"}]}


def rag_node(state: PricingState) -> Dict[str, Any]:
    from app.models.competitor_product import CompetitorProduct
    from app.models.product_catalog import ProductCatalog
    from app.models.retailer_product import RetailerProduct
    from app.services.ai.chroma_service import index_product_catalog, index_competitor_price, retrieve_pricing_context

    db = state["db"]
    catalog = db.query(ProductCatalog).filter(ProductCatalog.id == state["catalog_id"]).first()
    competitors = db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == state["catalog_id"]).all()
    index_product_catalog(catalog.id, catalog.name, catalog.category or "", catalog.product_details or "")
    for item in competitors:
        index_competitor_price(catalog.id, item.id, item.platform_name, item.product_name, item.price, catalog.category or "")
    retailer_product = db.query(RetailerProduct).filter_by(id=state["retailer_product_id"]).first()
    context = retrieve_pricing_context(catalog.name, catalog.id, retailer_product_id=retailer_product.id, user_id=retailer_product.user_id)
    return {"competitors": competitors, "rag_context": context, "workflow": state.get("workflow", []) + [{"step": "RAG", "status": "COMPLETED"}]}


def strategist_node(state: PricingState) -> Dict[str, Any]:
    from app.models.retailer_product import RetailerProduct
    from app.services.llm_service import generate_structured

    db = state["db"]
    owned = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    comps = state.get("competitors", [])
    prices = [float(c.price) for c in comps if c.price and math.isfinite(float(c.price))]
    minimum = owned.cost_price * (1 + owned.minimum_profit_margin / 100)
    import json
    context = {
        "product": owned.catalog_product.name,
        "category": owned.catalog_product.category,
        "details": owned.catalog_product.product_details,
        "cost_price": owned.cost_price,
        "minimum_margin_percent": owned.minimum_profit_margin,
        "stock_quantity": owned.stock_quantity,
        "competitors": [{"marketplace": c.platform_name, "name": c.product_name, "price": c.price} for c in comps],
        "marketplace_statuses": state.get("marketplace_statuses", {}),
        "rag_context": state.get("rag_context", ""),
    }
    prompt = "Return JSON fields recommended_price, reasoning_summary, competitor_summary, confidence, warnings. Use only this evidence. The price must be at least cost_price * (1 + minimum_margin_percent/100). If no competitors exist, say evidence is unavailable.\n" + json.dumps(context, default=str)
    response = generate_structured(prompt, model="qwen3:8b")
    recommendation = response.get("data") if response.get("success") else None
    try:
        model_price = float(recommendation.get("recommended_price")) if recommendation else float("nan")
        if not math.isfinite(model_price) or model_price <= 0:
            recommendation = None
    except (TypeError, ValueError):
        recommendation = None
    if not recommendation:
        # Deterministic evidence-based fallback; no competitor price is invented.
        price = max(minimum, sorted(prices)[len(prices) // 2] if prices else minimum)
        recommendation = {
            "recommended_price": round(price, 2),
            "reasoning_summary": "Price meets your minimum profit margin; the local Qwen service was unavailable.",
            "competitor_summary": f"Compared with {len(prices)} stored competitor observations." if prices else "No competitor observations are available.",
            "confidence": 0.45 if prices else 0.2,
            "warnings": (["Qwen strategist unavailable; deterministic fallback used."] + ([] if prices else ["Competitor evidence is unavailable."])),
        }
    recommendation["recommended_price"] = float(recommendation["recommended_price"])
    recommendation["competitor_count"] = len(prices)
    recommendation["expected_margin"] = ((float(recommendation["recommended_price"]) - owned.cost_price) / owned.cost_price * 100) if owned.cost_price else 0
    try:
        recommendation["confidence"] = min(1.0, max(0.0, float(recommendation.get("confidence", 0.5))))
    except (TypeError, ValueError):
        recommendation["confidence"] = 0.25
    if not isinstance(recommendation.get("warnings"), list):
        recommendation["warnings"] = []
    statuses = state.get("marketplace_statuses", {})
    unavailable = [market for market in ("Amazon", "Flipkart", "Myntra", "Meesho") if statuses.get(market) in {"BLOCKED", "TIMEOUT", "NETWORK_ERROR", "PARSE_ERROR", "EMPTY"}]
    if unavailable:
        recommendation["warnings"].append("Marketplace coverage was incomplete for: " + ", ".join(unavailable) + ".")
    return {"strategy": recommendation, "workflow": state.get("workflow", []) + [{"step": "STRATEGIST", "status": "COMPLETED"}]}


def compliance_node(state: PricingState) -> Dict[str, Any]:
    from app.models.retailer_product import RetailerProduct
    from app.models.recommendation import Recommendation
    db = state["db"]
    product = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    strategy = state.get("strategy", {})
    try:
        price = float(strategy.get("recommended_price"))
        valid = math.isfinite(price) and price > 0
    except (TypeError, ValueError):
        price, valid = 0.0, False
    floor = product.cost_price * (1 + product.minimum_profit_margin / 100)
    reasons = []
    if not valid: reasons.append("Recommended price is invalid.")
    if valid and price + 1e-8 < floor: reasons.append("Recommended price does not meet the minimum profit margin.")
    if not state.get("competitors"): reasons.append("No competitor evidence was available; recommendation is not approved.")
    if reasons:
        return {"compliance": {"accepted": False, "reasons": reasons}, "workflow": state.get("workflow", []) + [{"step": "COMPLIANCE", "status": "FAILED"}]}
    rec = Recommendation(retailer_product_id=product.id, recommended_price=price, expected_profit=price-product.cost_price, profit_percentage=(price-product.cost_price)/product.cost_price*100 if product.cost_price else 0, reasoning=strategy.get("reasoning_summary", ""), confidence_score=float(strategy.get("confidence", 0.5)), accepted_by_user=False)
    db.add(rec); db.commit(); db.refresh(rec)
    try:
        from app.services.ai.chroma_service import index_pricing_recommendation
        index_pricing_recommendation(
            retailer_product_id=product.id,
            catalog_product_id=product.catalog_product_id,
            product_name=product.catalog_product.name,
            recommended_price=price,
            reasoning=rec.reasoning,
            user_id=product.user_id,
        )
    except Exception:
        pass  # PostgreSQL remains authoritative; RAG indexing is best-effort.
    return {"recommendation": rec, "compliance": {"accepted": True, "reasons": []}, "workflow": state.get("workflow", []) + [{"step": "COMPLIANCE", "status": "COMPLETED"}]}


def build_pricing_graph():
    graph = StateGraph(PricingState)
    graph.add_node("identity", identity_node)
    graph.add_node("catalog", catalog_node)
    graph.add_node("scout", scout_node)
    graph.add_node("rag", rag_node)
    graph.add_node("strategist", strategist_node)
    graph.add_node("compliance", compliance_node)
    graph.add_edge(START, "identity")
    graph.add_conditional_edges("identity", route_identity, {"stop": END, "catalog": "catalog"})
    graph.add_conditional_edges("catalog", route_freshness, {"rag": "rag", "scout": "scout"})
    graph.add_edge("scout", "rag")
    graph.add_edge("rag", "strategist")
    graph.add_edge("strategist", "compliance")
    graph.add_edge("compliance", END)
    return graph.compile()


pricing_graph = build_pricing_graph()


def analyze_retailer_product(db: Session, retailer_product_id: int) -> Dict[str, Any]:
    return pricing_graph.invoke({"db": db, "retailer_product_id": retailer_product_id})
