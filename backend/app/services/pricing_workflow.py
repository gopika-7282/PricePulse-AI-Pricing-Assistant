"""LangGraph orchestration for the four PricePulse agents."""
from __future__ import annotations

import math
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class PricingState(TypedDict, total=False):
    db: Session
    retailer_product_id: int
    identity: Dict[str, Any]
    catalog_id: int
    fresh: bool
    identity_error: str
    marketplace_statuses: Dict[str, Any]
    competitors: List[Any]
    rag_context: str
    strategy: Dict[str, Any]
    compliance: Dict[str, Any]
    workflow: List[Dict[str, str]]
    error: str
    recommendation: Any
    freshness_status: str
    evidence_type: str
    freshness_started_at: datetime
    warnings: List[str]
    progress_callback: Callable[[Dict[str, str]], None]
    cancel_event: Any


class PricingWorkflowCancelled(Exception):
    """Cooperative cancellation between graph nodes."""


def _business_price_reasoning(product, price: float, minimum: float, market_prices: List[float],
                              comparable_count: int, unnormalized_count: int = 0,
                              evidence_type: str = "NO_EVIDENCE", estimator_fallback: bool = False) -> str:
    """Build concise retailer-facing reasons from verified prices and retailer inputs."""
    cost = float(product.cost_price or 0)
    gross_profit = price - cost
    actual_margin = gross_profit / cost * 100 if cost > 0 else 0.0
    margin_headroom = actual_margin - float(product.minimum_profit_margin or 0)
    pack = (f" for your {float(product.quantity_value):g} {product.quantity_unit} pack"
            if product.quantity_value and product.quantity_unit else " for your listed pack")
    parts = [
        f"At ₹{price:,.2f}{pack}, the price is ₹{price - minimum:,.2f} above your ₹{minimum:,.2f} minimum selling-price floor and gives ₹{gross_profit:,.2f} gross profit per pack ({actual_margin:.1f}% margin on cost, {margin_headroom:.1f} percentage points above your minimum)."
    ]
    if market_prices:
        low, high = min(market_prices), max(market_prices)
        basis = (f"per {float(product.quantity_value):g} {product.quantity_unit}"
                 if product.quantity_value and product.quantity_unit else "at listed pack sizes")
        if price < low:
            position = "below"
        elif price > high:
            position = "above"
        elif math.isclose(low, high, abs_tol=0.01):
            position = "aligned with"
        else:
            position = "within"
        parts.append(
            f"{comparable_count} compatible competitor listing(s) span ₹{low:,.2f}–₹{high:,.2f} {basis}; your price is {position} that observed range."
        )
        if evidence_type == "STALE_FALLBACK":
            parts.append("Live marketplace prices were unavailable; these observations are stored competitor data and may be out of date.")
    elif unnormalized_count:
        parts.append(
            f"{unnormalized_count} listing(s) had no usable pack quantity, so their raw prices were treated as lower-confidence context and excluded from equivalent-pack comparisons."
        )
    else:
        if estimator_fallback:
            parts.append("This provisional estimate uses retailer product and cost inputs because no quantity-comparable live market evidence was available.")
        elif evidence_type == "STALE_FALLBACK":
            parts.append("Live marketplace prices were unavailable, so this is an estimate based on retailer product and cost inputs.")
        else:
            parts.append("No quantity-comparable competitor evidence was available; this estimate uses retailer product and cost inputs.")
    if product.brand or product.category:
        context = " / ".join(str(value).strip() for value in (product.brand, product.category) if value and str(value).strip())
        parts.append(f"The supplied {context} product context and stock of {int(product.stock_quantity or 0)} pack(s) were included; no sales-velocity data was available.")
    elif product.stock_quantity is not None:
        parts.append(f"Your listed inventory is {int(product.stock_quantity)} pack(s); no sales-velocity data was available to infer demand.")
    return " ".join(parts[:4])
def _tracked_node(stage: str, message: str, node):
    """Publish real LangGraph node start/end events through an optional callback."""
    def run(state: PricingState) -> Dict[str, Any]:
        started = time.monotonic()
        emit = state.get("progress_callback")
        cancel_event = state.get("cancel_event")
        if cancel_event and cancel_event.is_set():
            raise PricingWorkflowCancelled()
        if emit:
            emit({"stage": stage, "status": "running", "message": message})
        try:
            result = node(state)
            if cancel_event and cancel_event.is_set():
                raise PricingWorkflowCancelled()
        except Exception as exc:
            logger.error("Pricing workflow stage failed: stage=%s error_type=%s", stage, type(exc).__name__)
            logger.info("Pricing workflow stage timing: stage=%s status=failed duration_ms=%d", stage, int((time.monotonic() - started) * 1000))
            if emit:
                emit({"stage": stage, "status": "failed", "message": message})
            raise
        failed = bool(result.get("error"))
        if stage == "recommendation" and not result.get("strategy"):
            failed = True
        if stage == "margin_review" and not (result.get("compliance") or {}).get("accepted", False):
            failed = True
        if stage == "persistence" and not result.get("recommendation"):
            failed = True
        if emit:
            emit({"stage": stage, "status": "failed" if failed else "completed", "message": message})
        logger.info("Pricing workflow stage timing: stage=%s status=%s duration_ms=%d", stage, "failed" if failed else "completed", int((time.monotonic() - started) * 1000))
        return result
    return run


def identity_node(state: PricingState) -> Dict[str, Any]:
    from app.models.retailer_product import RetailerProduct
    from app.services.product_identity_service import evaluate_product_identity, IdentityServiceUnavailable

    db = state["db"]
    owned = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    if not owned:
        return {"error": "Product not found", "workflow": [{"step": "IDENTITY", "status": "FAILED"}]}
    try:
        excluded = {owned.catalog_product_id} if owned.catalog_identity_staging else None
        identity = evaluate_product_identity(
            db, owned.product_name, owned.product_details or "", owned.category or "", owned.brand or "",
            exclude_catalog_ids=excluded,
            cost_price=owned.cost_price, stock_quantity=owned.stock_quantity,
            minimum_profit_margin=owned.minimum_profit_margin,
        )
    except IdentityServiceUnavailable as exc:
        message = "Product identity analysis is temporarily unavailable. Please try again later."
        return {"identity_error": message, "error": message,
                "workflow": [{"step": "IDENTITY", "status": "FAILED"}]}
    return {"identity": identity.model_dump(mode="json"),
            "workflow": state.get("workflow", []) + [{"step": "IDENTITY", "status": "COMPLETED"}]}


def route_identity(state: PricingState) -> str:
    return "stop" if state.get("error") else "catalog"


def catalog_decision_node(state: PricingState) -> Dict[str, Any]:
    """Resolve shared catalog identity only after the identity model responds."""
    from app.models.competitor_product import CompetitorProduct
    from app.models.product_catalog import ProductCatalog
    from app.models.retailer_product import RetailerProduct

    db = state["db"]
    product = db.query(RetailerProduct).filter_by(id=state["retailer_product_id"]).first()
    identity = state.get("identity") or {}
    decision = identity.get("decision")
    old_catalog = product.catalog_product
    old_catalog_was_staging = product.catalog_identity_staging
    if decision == "MATCH":
        catalog = db.query(ProductCatalog).filter_by(id=identity.get("matched_catalog_id")).first()
        if catalog is None:
            return {"error": "The selected product identity is no longer available.",
                    "workflow": state.get("workflow", []) + [{"step": "CATALOG", "status": "FAILED"}]}
    elif decision in {"NOT_MATCH", "UNCERTAIN_MATCH"}:
        def norm(value):
            return " ".join((value or "").casefold().split())
        def same_identity(candidate):
            return (
                norm(candidate.name) == norm(product.product_name)
                and norm(candidate.category) == norm(product.category)
                and norm(candidate.product_details) == norm(product.product_details)
            )
        if not product.catalog_identity_pending and same_identity(old_catalog):
            # An already-resolved product can be analyzed repeatedly without
            # creating another identical catalog row. New/edited identities
            # honor NOT_MATCH and UNCERTAIN_MATCH by creating a new catalog.
            catalog = old_catalog
        else:
            catalog = ProductCatalog(name=product.product_name, category=product.category,
                                     brand=product.brand, product_details=product.product_details)
            db.add(catalog)
            db.flush()
    else:
        return {"error": "Product identity analysis did not return a usable decision.",
                "workflow": state.get("workflow", []) + [{"step": "CATALOG", "status": "FAILED"}]}

    prior_catalog_id = product.catalog_product_id
    product.catalog_product_id = catalog.id
    product.catalog_identity_pending = False
    product.catalog_identity_staging = False
    db.commit()
    db.refresh(product)
    if old_catalog_was_staging and prior_catalog_id != catalog.id and old_catalog is not None:
        still_used = db.query(RetailerProduct.id).filter_by(catalog_product_id=prior_catalog_id).first()
        has_observations = db.query(CompetitorProduct.id).filter_by(catalog_product_id=prior_catalog_id).first()
        if not still_used and not has_observations:
            db.delete(old_catalog)
            db.commit()
    return {"catalog_id": catalog.id,
            "identity": {**identity, "catalog_product_id": catalog.id},
            "workflow": state.get("workflow", []) + [{"step": "CATALOG", "status": "COMPLETED"}]}


def freshness_node(state: PricingState) -> Dict[str, Any]:
    from app.models.product_catalog import ProductCatalog
    from app.services.competitor_service import get_competitor_freshness

    freshness = get_competitor_freshness(state["db"], state["catalog_id"])
    catalog = state["db"].query(ProductCatalog).filter_by(id=state["catalog_id"]).first()
    statuses = catalog.marketplace_statuses or {}
    fresh = freshness["status"] == "FRESH"
    update = {"freshness_status": freshness["status"], "marketplace_statuses": statuses,
              "fresh": fresh,
              "workflow": state.get("workflow", []) + [{"step": "FRESHNESS", "status": "COMPLETED"}]}
    if fresh:
        update["evidence_type"] = "LIVE"
    return update


def route_freshness(state: PricingState) -> str:
    return "stop" if state.get("error") else ("rag" if state.get("fresh") else "scout")


def route_catalog_decision(state: PricingState) -> str:
    return "stop" if state.get("error") else "freshness"


def scout_node(state: PricingState) -> Dict[str, Any]:
    from app.models.product_catalog import ProductCatalog
    from app.services.scraping_service import scrape_product

    catalog = state["db"].query(ProductCatalog).filter(ProductCatalog.id == state["catalog_id"]).first()
    scrape_started_at = datetime.now(timezone.utc)
    try:
        scrape_product(state["db"], catalog)
    except Exception:
        state["db"].rollback()
        statuses = {market: "NETWORK_ERROR" for market in ("Amazon", "Flipkart", "Myntra", "Meesho")}
    else:
        state["db"].refresh(catalog)
        statuses = catalog.marketplace_statuses or {}
    statuses = {name: ("SUCCESS" if str(status).upper() == "OK" else str(status).upper()) for name, status in statuses.items()}
    from app.models.competitor_product import CompetitorProduct
    stored = state["db"].query(CompetitorProduct).filter_by(catalog_product_id=state["catalog_id"]).all()
    live = []
    successful_platforms = {name.casefold() for name, status in statuses.items() if status == "SUCCESS"}
    for item in stored:
        observed = item.scraped_at
        if observed and observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if observed and observed >= scrape_started_at and item.platform_name.casefold() in successful_platforms:
            live.append(item)
    if live:
        evidence_type, competitors = "LIVE", live
        freshness_status = "FRESH"
    elif stored:
        evidence_type, competitors = "STALE_FALLBACK", stored
        freshness_status = "STALE"
    else:
        evidence_type, competitors = "NO_EVIDENCE", []
        freshness_status = "NO_EVIDENCE"
    return {"marketplace_statuses": statuses, "competitors": competitors,
            "freshness_started_at": scrape_started_at, "freshness_status": freshness_status,
            "evidence_type": evidence_type,
            "workflow": state.get("workflow", []) + [{"step": "SCOUT", "status": "COMPLETED" if live else "FAILED"}]}


def rag_node(state: PricingState) -> Dict[str, Any]:
    from app.models.competitor_product import CompetitorProduct
    from app.models.product_catalog import ProductCatalog
    from app.models.retailer_product import RetailerProduct
    from app.services.ai.chroma_service import index_product_catalog, index_competitor_price, index_retailer_product, retrieve_pricing_context

    db = state["db"]
    catalog = db.query(ProductCatalog).filter(ProductCatalog.id == state["catalog_id"]).first()
    competitors = state.get("competitors")
    if competitors is None:
        competitors = db.query(CompetitorProduct).filter(CompetitorProduct.catalog_product_id == state["catalog_id"]).all()
    index_product_catalog(catalog.id, catalog.name, catalog.category or "", catalog.product_details or "")
    for item in competitors:
        index_competitor_price(
            catalog.id, item.id, item.platform_name, item.product_name, item.price,
            catalog.category or "", extra_meta={
                "product_url": item.product_url,
                "product_details": item.product_details,
                "rating": item.rating,
                "scraped_at": item.scraped_at.isoformat() if item.scraped_at else None,
            },
        )
    retailer_product = db.query(RetailerProduct).filter_by(id=state["retailer_product_id"]).first()
    index_retailer_product(
        retailer_product_id=retailer_product.id, user_id=retailer_product.user_id,
        catalog_product_id=retailer_product.catalog_product_id,
        product_name=retailer_product.product_name, category=retailer_product.category or "",
        brand=retailer_product.brand or "", product_details=retailer_product.product_details or "",
        cost_price=retailer_product.cost_price, stock_quantity=retailer_product.stock_quantity,
        minimum_profit_margin=retailer_product.minimum_profit_margin,
        quantity_value=retailer_product.quantity_value, quantity_unit=retailer_product.quantity_unit,
    )
    context = retrieve_pricing_context(
        " ".join(filter(None, [
            retailer_product.product_name, retailer_product.category,
            retailer_product.product_details, retailer_product.brand,
        ])), catalog.id, retailer_product_id=retailer_product.id,
        user_id=retailer_product.user_id,
        competitor_product_ids=[item.id for item in competitors],
    )
    return {"competitors": competitors, "rag_context": context, "workflow": state.get("workflow", []) + [{"step": "RAG", "status": "COMPLETED"}]}


def strategist_node(state: PricingState) -> Dict[str, Any]:
    import json
    from pydantic import BaseModel, Field, ValidationError
    from app.models.retailer_product import RetailerProduct
    from app.services.llm_service import generate_structured
    from app.services.pricing_service import minimum_viable_selling_price

    db = state["db"]
    owned = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    comps = state.get("competitors", [])
    from app.utils.quantity import normalize_quantity, price_at_target_pack
    prices = [float(c.price) for c in comps if c.price and math.isfinite(float(c.price))]
    unnormalized_comps = []
    target_pack = normalize_quantity(owned.quantity_value, owned.quantity_unit) if owned.quantity_value and owned.quantity_unit else None
    if target_pack:
        dimensionally_comparable = []
        for competitor in comps:
            equivalent_price = price_at_target_pack(float(competitor.price), owned.quantity_value, owned.quantity_unit,
                competitor.quantity_value, competitor.quantity_unit, competitor.pack_count,
                competitor.total_quantity, competitor.total_quantity_unit)
            observed_value = competitor.total_quantity if competitor.total_quantity is not None else competitor.quantity_value
            observed_unit = competitor.total_quantity_unit or competitor.quantity_unit
            observed_count = 1 if competitor.total_quantity is not None else (competitor.pack_count or 1)
            normalized = normalize_quantity(float(observed_value) * observed_count, observed_unit) if observed_value and observed_unit else None
            if equivalent_price is not None and normalized and normalized[1] == target_pack[1]:
                competitor._target_pack_price = equivalent_price
                competitor._same_pack = math.isclose(normalized[0], target_pack[0], rel_tol=0.01)
                dimensionally_comparable.append(competitor)
            elif not observed_value or not observed_unit:
                # Preserve the observed raw price as explicitly weak evidence;
                # it is never mixed into the equivalent-pack price list.
                unnormalized_comps.append(competitor)
        if dimensionally_comparable:
            # Keep every compatible pack's target-size equivalent in market
            # evidence so dispersion and position reflect the whole market.
            comps = dimensionally_comparable
            prices = [c._target_pack_price for c in comps]
        else:
            comps, prices = [], []
    minimum = minimum_viable_selling_price(owned.cost_price, owned.minimum_profit_margin)
    evidence_type = state.get("evidence_type", "NO_EVIDENCE")
    distinct_platforms = {str(c.platform_name).casefold() for c in comps}
    if evidence_type == "STALE_FALLBACK" and len(distinct_platforms) < 2:
        # A thin stale snapshot is not strong enough for competitive pricing.
        # Continue through the normal estimate path with product and RAG context.
        comps = []
        prices = []
        evidence_type = "NO_EVIDENCE"
    if evidence_type != "NO_EVIDENCE" and not prices and not unnormalized_comps:
        # Listings with incompatible dimensions (for example, ml vs g) cannot
        # support this pack's market comparison. Use the estimate path instead.
        evidence_type = "NO_EVIDENCE"
    if evidence_type != "NO_EVIDENCE" and not prices and not unnormalized_comps:
        return {"strategy": {}, "evidence_type": "NO_EVIDENCE",
                "warnings": ["No verified competitor prices were available to support a recommendation."],
                "workflow": state.get("workflow", []) + [{"step": "STRATEGIST", "status": "FAILED"}]}

    if evidence_type == "NO_EVIDENCE":
        class PriceEstimate(BaseModel):
            estimated_price: float = Field(gt=0)
            price_range_low: float = Field(gt=0)
            price_range_high: float = Field(gt=0)
            confidence: float = Field(ge=0, le=1)
            reasoning: str = Field(min_length=1, max_length=300)
            evidence_type: str

        product = owned.catalog_product
        estimate_prompt = (
            "Produce an approximate e-commerce selling price estimate using the supplied product, description, pack quantity and unit, cost, inventory, margin, and relevant pricing context. "
            "Do not invent competitor prices, ratings, trends, or marketplace observations. Use only supplied evidence and general retail knowledge for the estimate. "
            "This is not a competitor observation. Return only structured fields requested. Do not include chain-of-thought; reasoning must be one concise business sentence. "
            "Set evidence_type to LLM_ESTIMATE. Estimate a realistic starting price and range, not a market average.\n" + json.dumps({
                "product_name": owned.product_name, "category": owned.category, "brand": owned.brand,
                "product_details": owned.product_details, "cost_price": owned.cost_price,
                "quantity_value": owned.quantity_value, "quantity_unit": owned.quantity_unit,
                "stock_quantity": owned.stock_quantity,
                "minimum_profit_margin_percent": owned.minimum_profit_margin,
                "minimum_viable_price": round(minimum, 2),
                "relevant_pricing_context": state.get("rag_context") or "No relevant stored pricing context was retrieved.",
            }, default=str)
        )
        from app.config import OLLAMA_STRATEGIST_TIMEOUT
        response = generate_structured(estimate_prompt, schema=PriceEstimate.model_json_schema(), timeout=OLLAMA_STRATEGIST_TIMEOUT, operation="strategist_estimate")
        try:
            estimate = PriceEstimate.model_validate(response["data"]) if response.get("success") and response.get("data") else None
            if estimate and estimate.evidence_type != "LLM_ESTIMATE":
                estimate = None
            if estimate and not all(math.isfinite(value) for value in (
                estimate.estimated_price, estimate.price_range_low, estimate.price_range_high, estimate.confidence
            )):
                estimate = None
        except (ValidationError, TypeError, ValueError):
            estimate = None
        estimator_fallback = estimate is None
        if estimator_fallback:
            # A failed LLM call is still a pricing estimate path: the margin floor
            # is deterministic, with an explicit provisional range around it.
            from types import SimpleNamespace
            estimate = SimpleNamespace(estimated_price=minimum * 1.10,
                price_range_low=minimum, price_range_high=minimum * 1.30,
                confidence=0.20, reasoning="Cost and minimum margin set the floor; this provisional range needs market validation.",
                evidence_type="LLM_ESTIMATE")
        estimated_price = math.ceil(max(minimum, estimate.estimated_price) * 100 - 1e-9) / 100
        range_low = math.ceil(min(estimated_price, max(minimum, estimate.price_range_low)) * 100 - 1e-9) / 100
        range_high = math.ceil(max(range_low, estimate.price_range_high, estimated_price) * 100 - 1e-9) / 100
        strategy = {
            "recommended_price": round(estimated_price, 2),
            "price_range_low": round(range_low, 2), "price_range_high": round(range_high, 2),
            "reasoning_summary": _business_price_reasoning(
                owned, estimated_price, minimum, [], 0, evidence_type=evidence_type,
                estimator_fallback=estimator_fallback,
            ),
            "competitor_summary": "Estimated from product information; no competitor prices were used.",
            "confidence": min(0.50, estimate.confidence), "warnings": [], "competitor_count": 0,
            "expected_margin": ((estimated_price - owned.cost_price) / owned.cost_price * 100) if owned.cost_price else 0,
            "margin_headroom": ((estimated_price - owned.cost_price) / owned.cost_price * 100 - float(owned.minimum_profit_margin or 0)) if owned.cost_price else 0,
            "minimum_viable_price": round(minimum, 2),
            "evidence_type": "LLM_ESTIMATE",
        }
        return {"strategy": strategy, "evidence_type": "LLM_ESTIMATE",
                "workflow": state.get("workflow", []) + [{"step": "STRATEGIST", "status": "COMPLETED"}]}

    context = {
        "product": owned.product_name,
        "category": owned.category,
        "details": owned.product_details,
        "quantity_value": owned.quantity_value,
        "quantity_unit": owned.quantity_unit,
        "cost_price": owned.cost_price,
        "minimum_margin_percent": owned.minimum_profit_margin,
        "stock_quantity": owned.stock_quantity,
        "competitors": [{"marketplace": c.platform_name, "name": c.product_name, "product_url": c.product_url,
                         "price": c.price, "rating": c.rating, "availability": c.availability,
                         "price_at_target_pack_size": getattr(c, "_target_pack_price", None),
                         "same_pack": getattr(c, "_same_pack", None),
                         "quantity_value": c.quantity_value, "quantity_unit": c.quantity_unit,
                         "pack_count": c.pack_count, "total_quantity": c.total_quantity,
                         "total_quantity_unit": c.total_quantity_unit} for c in comps],
        "raw_market_evidence_without_pack_quantity": [
            {"marketplace": c.platform_name, "name": c.product_name, "product_url": c.product_url,
             "observed_price": c.price, "rating": c.rating, "availability": c.availability,
             "note": "Lower-confidence raw price only; quantity is unavailable and this is excluded from normalized price ranges."}
            for c in unnormalized_comps
        ],
        "marketplace_statuses": state.get("marketplace_statuses", {}),
    }
    prompt = "Return JSON fields recommended_price, competitor_summary, confidence, warnings. Use only the verified competitor evidence supplied. Prioritize same-pack listings; when sizes differ, compare unit prices only within compatible dimensions and preserve observed pack-size economics. Raw prices without quantity are lower-confidence context and must not be normalized or used in a normalized range. The price must be at least cost_price * (1 + minimum_margin_percent/100). Do not include chain-of-thought.\n" + json.dumps(context, default=str)
    from app.config import OLLAMA_STRATEGIST_TIMEOUT
    response = generate_structured(prompt, timeout=OLLAMA_STRATEGIST_TIMEOUT, operation="strategist_competitor_pricing")
    recommendation = response.get("data") if response.get("success") else None
    try:
        model_price = float(recommendation.get("recommended_price")) if recommendation else float("nan")
        if not math.isfinite(model_price) or model_price <= 0:
            recommendation = None
    except (TypeError, ValueError):
        recommendation = None
    if not recommendation:
        return {"strategy": {}, "evidence_type": evidence_type,
                "error": "Price analysis could not be completed right now. Please try again.",
                "workflow": state.get("workflow", []) + [{"step": "STRATEGIST", "status": "FAILED"}]}
    if recommendation:
        recommendation["recommended_price"] = float(recommendation["recommended_price"])
        recommendation["competitor_count"] = len(prices) + len(unnormalized_comps)
        try:
            recommendation["confidence"] = min(1.0, max(0.0, float(recommendation.get("confidence", 0.5))))
        except (TypeError, ValueError):
            recommendation["confidence"] = 0.25
        if not isinstance(recommendation.get("warnings"), list):
            recommendation["warnings"] = []
        statuses = state.get("marketplace_statuses", {})
        if evidence_type == "STALE_FALLBACK":
            recommendation["reasoning_summary"] = "Live marketplace prices were unavailable, so the recommendation uses the latest stored competitor observations."
            recommendation["warnings"].append("Live marketplace prices were unavailable; this recommendation uses stored competitor prices that may be out of date.")
            recommendation["confidence"] = min(float(recommendation.get("confidence", 0.4)), 0.55)
        else:
            recommendation["reasoning_summary"] = "Recommendation is based on verified competitor prices from the available marketplaces."
            platform_count = len(distinct_platforms)
            evidence_cap = min(0.90, 0.55 + max(0, platform_count - 1) * 0.10)
            recommendation["confidence"] = min(float(recommendation.get("confidence", 0.5)), evidence_cap)
        recommendation["evidence_type"] = evidence_type
        recommended_price = max(minimum, float(recommendation["recommended_price"]))
        recommendation["recommended_price"] = recommended_price
        recommendation["expected_margin"] = ((recommended_price - owned.cost_price) / owned.cost_price * 100) if owned.cost_price else 0
        recommendation["margin_headroom"] = recommendation["expected_margin"] - float(owned.minimum_profit_margin or 0)
        recommendation["minimum_viable_price"] = round(minimum, 2)
        recommendation["market_price_range_low"] = round(min(prices), 2) if prices else None
        recommendation["market_price_range_high"] = round(max(prices), 2) if prices else None
        if prices:
            # Persisted recommendation bounds must contain the chosen price;
            # keep the verified market-only range separately above.
            recommendation["price_range_low"] = round(min(recommended_price, min(prices)), 2)
            recommendation["price_range_high"] = round(max(recommended_price, max(prices)), 2)
        elif unnormalized_comps:
            recommendation["price_range_low"] = None
            recommendation["price_range_high"] = None
        if unnormalized_comps:
            recommendation["warnings"].append(
                f"{len(unnormalized_comps)} competitor price(s) lacked pack quantity and were used only as lower-confidence raw context."
            )
            recommendation["confidence"] = min(float(recommendation["confidence"]), 0.35)
        recommendation["reasoning_summary"] = _business_price_reasoning(
            owned, recommended_price, minimum, prices, len(comps), len(unnormalized_comps),
            evidence_type=evidence_type,
        )
    return {"strategy": recommendation or {}, "evidence_type": evidence_type,
            "workflow": state.get("workflow", []) + [{"step": "STRATEGIST", "status": "COMPLETED" if recommendation else "FAILED"}]}


def compliance_node(state: PricingState) -> Dict[str, Any]:
    from app.models.retailer_product import RetailerProduct
    from app.services.pricing_service import minimum_viable_selling_price
    db = state["db"]
    product = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    strategy = state.get("strategy", {})
    try:
        price = float(strategy.get("recommended_price"))
        valid = math.isfinite(price) and price > 0
    except (TypeError, ValueError):
        price, valid = 0.0, False
    floor = minimum_viable_selling_price(product.cost_price, product.minimum_profit_margin)
    reasons = []
    if not valid: reasons.append("Recommended price is invalid.")
    if valid and price + 1e-8 < floor: reasons.append("Recommended price does not meet the minimum profit margin.")
    evidence_type = state.get("evidence_type", strategy.get("evidence_type", "NO_EVIDENCE"))
    if not state.get("competitors") and evidence_type != "LLM_ESTIMATE":
        reasons.append("No competitor evidence or usable estimate was available.")
    try:
        confidence = min(1.0, max(0.0, float(strategy.get("confidence", 0.4))))
    except (TypeError, ValueError):
        confidence = 0.35
    confidence_caps = {"LIVE": 0.90, "STALE_FALLBACK": 0.55, "LLM_ESTIMATE": 0.50, "NO_EVIDENCE": 0.0}
    confidence = min(confidence, confidence_caps.get(evidence_type, 0.4))
    if reasons:
        return {"compliance": {"accepted": False, "reasons": reasons}, "workflow": state.get("workflow", []) + [{"step": "COMPLIANCE", "status": "FAILED"}]}
    return {"compliance": {"accepted": True, "reasons": [], "confidence": confidence},
            "workflow": state.get("workflow", []) + [{"step": "COMPLIANCE", "status": "COMPLETED"}]}


def persistence_node(state: PricingState) -> Dict[str, Any]:
    """Persist only a recommendation that passed the separate compliance stage."""
    if not (state.get("compliance") or {}).get("accepted"):
        return {"error": "Recommendation did not pass compliance.",
                "workflow": state.get("workflow", []) + [{"step": "PERSISTENCE", "status": "FAILED"}]}
    from app.models.retailer_product import RetailerProduct
    from app.models.recommendation import Recommendation
    db = state["db"]
    product = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    strategy = state.get("strategy", {})
    price = float(strategy["recommended_price"])
    evidence_type = state.get("evidence_type", strategy.get("evidence_type", "NO_EVIDENCE"))
    confidence = float((state.get("compliance") or {}).get("confidence", strategy.get("confidence", 0.4)))
    raw_range_low = strategy.get("price_range_low", price)
    raw_range_high = strategy.get("price_range_high", price)
    if (raw_range_low is None) != (raw_range_high is None):
        return {"error": "Recommendation price range is invalid.",
                "workflow": state.get("workflow", []) + [{"step": "PERSISTENCE", "status": "FAILED"}]}
    price_range_low = float(raw_range_low) if raw_range_low is not None else None
    price_range_high = float(raw_range_high) if raw_range_high is not None else None
    if price_range_low is not None and not (
            math.isfinite(price_range_low) and math.isfinite(price_range_high)
            and price_range_low <= price <= price_range_high):
        return {"error": "Recommendation price range is invalid.",
                "workflow": state.get("workflow", []) + [{"step": "PERSISTENCE", "status": "FAILED"}]}
    rec = Recommendation(retailer_product_id=product.id, recommended_price=price,
        recommended_price_min=price_range_low, recommended_price_max=price_range_high,
        expected_profit=price-product.cost_price,
        profit_percentage=(price-product.cost_price)/product.cost_price*100 if product.cost_price else 0,
        reasoning=strategy.get("reasoning_summary", ""), confidence_score=confidence,
        evidence_type=evidence_type, accepted_by_user=False)
    db.add(rec); db.commit(); db.refresh(rec)
    try:
        from app.services.ai.chroma_service import index_pricing_recommendation
        index_pricing_recommendation(
            retailer_product_id=product.id,
            catalog_product_id=product.catalog_product_id,
            product_name=product.product_name,
            recommended_price=price,
            reasoning=rec.reasoning,
            user_id=product.user_id,
            evidence_type=rec.evidence_type,
            price_range_min=rec.recommended_price_min,
            price_range_max=rec.recommended_price_max,
        )
    except Exception:
        pass  # PostgreSQL remains authoritative; RAG indexing is best-effort.
    return {"recommendation": rec, "workflow": state.get("workflow", []) + [{"step": "PERSISTENCE", "status": "COMPLETED"}]}


def build_pricing_graph():
    graph = StateGraph(PricingState)
    graph.add_node("identity", _tracked_node("product_review", "Checking your product details", identity_node))
    graph.add_node("catalog_decision", _tracked_node("catalog_review", "Resolving the product catalog entry", catalog_decision_node))
    graph.add_node("freshness", _tracked_node("market_review", "Reviewing available market information", freshness_node))
    graph.add_node("scout", _tracked_node("market_check", "Checking available market prices", scout_node))
    graph.add_node("rag", _tracked_node("context_review", "Reviewing relevant product and market information", rag_node))
    graph.add_node("strategist", _tracked_node("recommendation", "Preparing your price recommendation", strategist_node))
    graph.add_node("compliance", _tracked_node("margin_review", "Checking your minimum profit margin", compliance_node))
    graph.add_node("persistence", _tracked_node("persistence", "Saving your recommendation", persistence_node))
    graph.add_edge(START, "identity")
    graph.add_conditional_edges("identity", route_identity, {"stop": END, "catalog": "catalog_decision"})
    graph.add_conditional_edges("catalog_decision", route_catalog_decision, {"stop": END, "freshness": "freshness"})
    graph.add_conditional_edges("freshness", route_freshness, {"stop": END, "rag": "rag", "scout": "scout"})
    graph.add_edge("scout", "rag")
    graph.add_edge("rag", "strategist")
    graph.add_edge("strategist", "compliance")
    graph.add_conditional_edges("compliance", lambda state: "persistence" if (state.get("compliance") or {}).get("accepted") else "stop", {"persistence": "persistence", "stop": END})
    graph.add_edge("persistence", END)
    return graph.compile()


pricing_graph = build_pricing_graph()


def analyze_retailer_product(db: Session, retailer_product_id: int, progress_callback=None, cancel_event=None) -> Dict[str, Any]:
    return pricing_graph.invoke({"db": db, "retailer_product_id": retailer_product_id,
                                 "progress_callback": progress_callback, "cancel_event": cancel_event})
