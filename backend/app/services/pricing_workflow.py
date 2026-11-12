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
    # Every analysis attempts the configured marketplace scrapers, even when
    # stored observations are recent. The scout node will retain stored rows
    # as stale fallback only when this scrape yields no fresh usable listings.
    return "stop" if state.get("error") else "scout"


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
    stored = state["db"].query(CompetitorProduct).filter(
        CompetitorProduct.catalog_product_id == state["catalog_id"],
        CompetitorProduct.observation_source == "SCRAPE",
        CompetitorProduct.scraped_at.isnot(None),
    ).all()
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
    from app.utils.quantity import strip_quantity_marker
    from app.services.relevance_filter_service import strip_match_marker

    db = state["db"]
    catalog = db.query(ProductCatalog).filter(ProductCatalog.id == state["catalog_id"]).first()
    competitors = state.get("competitors")
    if competitors is None:
        competitors = db.query(CompetitorProduct).filter(
            CompetitorProduct.catalog_product_id == state["catalog_id"],
            CompetitorProduct.observation_source == "SCRAPE",
            CompetitorProduct.scraped_at.isnot(None),
        ).all()
    index_product_catalog(catalog.id, catalog.name, catalog.category or "", catalog.product_details or "")
    for item in competitors:
        index_competitor_price(
            catalog.id, item.id, item.platform_name, item.product_name, item.price,
            catalog.category or "", extra_meta={
                "product_url": item.product_url,
                "product_details": strip_match_marker(strip_quantity_marker(item.product_details)),
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


def _local_pricing_estimate(owned, statistics: Dict[str, Any], offers: List[Dict[str, Any]],
                           marketplace_statuses: Dict[str, Any]) -> tuple[Dict[str, Any] | None, str | None]:
    """Ask the configured local Ollama model for a clearly non-market estimate."""
    import json
    from app.config import OLLAMA_MODEL, OLLAMA_STRATEGIST_TIMEOUT
    from app.services.llm_service import generate_structured

    context = {
        "product_name": owned.product_name,
        "category": owned.category,
        "brand": owned.brand,
        "product_details": owned.product_details,
        "pack_size": {"value": owned.quantity_value, "unit": owned.quantity_unit},
        "cost_price": float(owned.cost_price),
        "minimum_profit_margin_percent": float(owned.minimum_profit_margin),
        "currency": "INR",
        "target_market": "India",
        "validated_market_evidence": {
            "comparable_offer_count": statistics.get("comparable_offer_count", 0),
            "marketplaces": statistics.get("marketplaces", []),
            "median_normalized_price": statistics.get("median_price"),
            "typical_normalized_range": [statistics.get("typical_price_range_low"), statistics.get("typical_price_range_high")],
            "offers": [
                {**offer, "statistical_outlier_flagged": any(
                    flagged.get("product_name") == offer.get("product_name")
                    and abs(float(flagged.get("price", 0)) - float(offer.get("price", 0))) <= 0.01
                    for flagged in statistics.get("outlier_offers", [])
                )}
                for offer in offers if not offer.get("exclusion_reason")
            ],
        },
        "relevant_pricing_context": "No RAG market context is asserted; only validated marketplace observations above are market evidence.",
        "marketplace_statuses": marketplace_statuses,
    }
    prompt = (
        "Estimate a plausible provisional selling-price range for this product using only its supplied product and business details. "
        "Validated offers, if supplied, are limited evidence and must remain distinct from your estimate. "
        "Do not claim to have checked any marketplace or current market prices. Do not invent observed prices or a market median. "
        "Return JSON with suggested_price, price_range_low, price_range_high, rationale, and assumptions.\n"
        + json.dumps(context, ensure_ascii=False, default=str)
    )
    try:
        response = generate_structured(
            prompt=prompt,
            model=OLLAMA_MODEL,
            timeout=OLLAMA_STRATEGIST_TIMEOUT,
            operation="pricing_fallback",
        )
    except Exception as exc:
        return None, f"Local pricing estimate failed ({type(exc).__name__})."
    if not response or not response.get("success") or not isinstance(response.get("data"), dict):
        return None, (response or {}).get("error") or "Local pricing estimate was unavailable."
    data = response["data"]
    try:
        suggested = float(data.get("suggested_price", data.get("recommended_price")))
        low = float(data.get("price_range_low", data.get("recommended_price_min")))
        high = float(data.get("price_range_high", data.get("recommended_price_max")))
    except (TypeError, ValueError):
        return None, "Local pricing estimate returned an incomplete price range."
    if not all(math.isfinite(value) and value > 0 for value in (low, suggested, high)) or not low <= suggested <= high:
        return None, "Local pricing estimate returned an invalid or contradictory price range."
    return {"suggested_price": suggested, "price_range_low": low, "price_range_high": high,
            "rationale": str(data.get("rationale", data.get("reasoning", ""))).strip(),
            "assumptions": str(data.get("assumptions", "")).strip()}, None


def strategist_node(state: PricingState) -> Dict[str, Any]:
    from app.models.retailer_product import RetailerProduct
    from app.services.pricing_service import minimum_viable_selling_price
    from app.services.market_statistics import (calculate_market_statistics, market_confidence,
                                               market_position, deduplicate_comparable_offers)

    db = state["db"]
    owned = db.query(RetailerProduct).filter(RetailerProduct.id == state["retailer_product_id"]).first()
    from app.utils.quantity import price_at_target_pack, listing_quantity_is_uncertain, strip_quantity_marker
    from app.services.relevance_filter_service import classify_candidate, strip_match_marker

    minimum = minimum_viable_selling_price(owned.cost_price, owned.minimum_profit_margin)
    minimum_display = math.ceil(minimum * 100 - 1e-9) / 100
    from app.utils.quantity import normalize_quantity
    target_pack = normalize_quantity(owned.quantity_value, owned.quantity_unit) if owned.quantity_value and owned.quantity_unit else None
    source_evidence = state.get("evidence_type", "NO_EVIDENCE")
    input_offers = state.get("competitors", [])
    normalized_offers = []
    exclusions = []
    for competitor in input_offers:
        try:
            raw_price = float(competitor.price)
        except (TypeError, ValueError):
            raw_price = float("nan")
        if not math.isfinite(raw_price) or raw_price <= 0:
            exclusions.append({"marketplace": competitor.platform_name, "exclusion_reason": "invalid_price"})
            continue
        match_quality, match_reason = classify_candidate(
            owned.product_name, competitor.product_name or "", owned.product_details or "",
            strip_match_marker(strip_quantity_marker(competitor.product_details)) or "",
        )
        if match_quality not in {"exact", "alternative"}:
            exclusions.append({"marketplace": competitor.platform_name,
                               "exclusion_reason": "incompatible_product" if match_quality == "rejected" else "uncertain_product_match",
                               "detail": match_reason})
            continue
        if listing_quantity_is_uncertain(competitor):
            exclusions.append({"marketplace": competitor.platform_name,
                               "exclusion_reason": "quantity_uncertain"})
            continue
        if not target_pack:
            exclusions.append({"marketplace": competitor.platform_name,
                               "exclusion_reason": "target_quantity_unavailable"})
            continue
        equivalent = price_at_target_pack(
            raw_price, owned.quantity_value, owned.quantity_unit,
            competitor.quantity_value, competitor.quantity_unit, competitor.pack_count,
            competitor.total_quantity, competitor.total_quantity_unit,
        )
        if equivalent is None or not math.isfinite(equivalent) or equivalent <= 0:
            observed_unit = competitor.total_quantity_unit or competitor.quantity_unit
            reason = "incompatible_quantity_dimension" if observed_unit else "quantity_unavailable"
            exclusions.append({"marketplace": competitor.platform_name, "exclusion_reason": reason})
            continue
        normalized = normalize_quantity(
            competitor.total_quantity if competitor.total_quantity is not None else
            (competitor.quantity_value * (competitor.pack_count or 1) if competitor.quantity_value is not None else None),
            competitor.total_quantity_unit or competitor.quantity_unit,
        )
        target_normalized = normalize_quantity(owned.quantity_value, owned.quantity_unit)
        normalized_offers.append({
            "price": equivalent, "marketplace": competitor.platform_name,
            "listed_price": raw_price,
            "match_quality": match_quality, "product_name": competitor.product_name,
            "product_url": competitor.product_url,
            "scraped_at": competitor.scraped_at,
            "pack_quantity": competitor.total_quantity if competitor.total_quantity is not None else competitor.quantity_value,
            "pack_unit": competitor.total_quantity_unit or competitor.quantity_unit,
            "pack_count": competitor.pack_count or 1,
            "quantity_dimension": normalized[1] if normalized else None,
            "normalized_quantity": normalized[0] if normalized else None,
            "same_pack": bool(normalized and target_normalized and normalized[1] == target_normalized[1]
                              and math.isclose(normalized[0], target_normalized[0], rel_tol=0.001)),
        })

    deduplicated_offers = deduplicate_comparable_offers(normalized_offers)
    statistics = calculate_market_statistics([
        *deduplicated_offers,
        *exclusions,
    ])
    offer_count = statistics["comparable_offer_count"]
    marketplace_count = statistics["distinct_marketplace_count"]
    stale = source_evidence == "STALE_FALLBACK"
    enough_evidence = offer_count >= 3 and marketplace_count >= 2
    pricing_method = "market_data_backed" if enough_evidence else "market_data_plus_local_ai" if offer_count else "local_ai_estimate"
    local_estimate = None
    estimation_error = None
    if not enough_evidence:
        local_estimate, estimation_error = _local_pricing_estimate(
            owned, statistics, deduplicated_offers,
            state.get("marketplace_statuses", {}),
        )

    reference = statistics["median_price"]
    if enough_evidence:
        evidence_type = "STALE_FALLBACK" if stale else "LIVE"
        raw_recommended_price = reference or minimum
        raw_range_low = statistics["typical_price_range_low"] or minimum
        raw_range_high = statistics["typical_price_range_high"] or raw_recommended_price
        confidence = market_confidence(offer_count, marketplace_count, stale=stale)
    elif local_estimate:
        evidence_type = "PROVISIONAL" if offer_count else "LLM_ESTIMATE"
        raw_recommended_price = local_estimate["suggested_price"]
        raw_range_low = local_estimate["price_range_low"]
        raw_range_high = local_estimate["price_range_high"]
        confidence = min(0.40 if offer_count else 0.35, market_confidence(offer_count, marketplace_count, stale=stale) if offer_count else 0.35)
    else:
        evidence_type = "ESTIMATION_UNAVAILABLE"
        pricing_method = "estimate_unavailable"
        raw_recommended_price = raw_range_low = raw_range_high = None
        confidence = 0.0

    if raw_recommended_price is not None:
        recommended_price = math.ceil(max(minimum, raw_recommended_price) * 100 - 1e-9) / 100
        range_low = round(max(minimum_display, min(raw_range_low, recommended_price)), 2)
        range_high = round(max(range_low, recommended_price, raw_range_high), 2)
    else:
        recommended_price = range_low = range_high = None
    confidence_label = "High" if confidence >= 0.75 else "Moderate" if confidence >= 0.55 else "Low"
    position = market_position(recommended_price, reference,
                               statistics["typical_price_range_low"], statistics["typical_price_range_high"]) if recommended_price is not None else None
    cost = float(owned.cost_price)
    profit = recommended_price - cost if recommended_price is not None else None
    markup = profit / cost * 100 if cost and profit is not None else 0.0
    gross_margin = profit / recommended_price * 100 if recommended_price else 0.0

    if recommended_price is None:
        summary = "Local AI estimate — no sufficient live market data. Estimation unavailable; the minimum profitable price is shown separately."
    elif enough_evidence:
        summary = (f"Recommended price is the higher of the ₹{minimum_display:,.2f} minimum profitable floor "
                   f"and the ₹{reference:,.2f} median comparable-market reference.")
    elif offer_count:
        summary = (f"Market data + local AI estimate. The local estimate was informed by {offer_count} validated offer(s) "
                   f"across {marketplace_count} marketplace(s); the normalized median reference is ₹{reference:,.2f}. "
                   "This is provisional and low confidence.")
    else:
        summary = "Local AI estimate — no sufficient live market data. This is a provisional product-and-cost estimate, not an observed market price."
    if local_estimate and local_estimate.get("rationale"):
        summary += " " + local_estimate["rationale"]
    if local_estimate and local_estimate.get("assumptions"):
        summary += " Assumptions: " + local_estimate["assumptions"]

    if stale:
        summary += " Live marketplace prices were unavailable; stored observations may be out of date."
    if statistics["outlier_count"]:
        summary += (f" {statistics['outlier_count']} price outlier(s) were flagged using "
                    f"{statistics['outlier_method']} ")
    if statistics["exclusion_count"]:
        reason_summary = ", ".join(f"{reason.replace('_', ' ')}: {count}"
                                   for reason, count in sorted(statistics["exclusion_reasons"].items()))
        summary += f" {statistics['exclusion_count']} offer(s) excluded ({reason_summary})."
    if recommended_price is not None:
        summary += (f" Gross profit is ₹{profit:,.2f} per pack; markup on cost is {markup:.1f}% and "
                f"gross margin on selling price is {gross_margin:.1f}%.")
    warning_list = []
    if estimation_error:
        warning_list.append("Local AI estimate unavailable: " + estimation_error)
    if offer_count and not enough_evidence:
        warning_list.append("Market evidence is sparse; local AI was used alongside the validated market observations.")
    if not offer_count:
        warning_list.append("No validated comparable offers were available; no market statistics are reported.")
    if stale:
        warning_list.append("Live marketplace prices were unavailable; stored observations may be out of date.")
    if statistics["outlier_count"]:
        warning_list.append(f"{statistics['outlier_count']} statistical outlier(s) are flagged but remain included in observed market statistics.")

    strategy = {
        "recommended_price": recommended_price,
        "price_range_low": range_low, "price_range_high": range_high,
        "minimum_viable_price": round(minimum_display, 2),
        "market_reference_price": reference,
        "market_price_range_low": statistics["observed_minimum_price"],
        "market_price_range_high": statistics["observed_maximum_price"],
        "market_statistics": statistics,
        "market_position": position,
        "gross_profit_per_unit": round(profit, 2) if profit is not None else None,
        "markup_on_cost_percent": round(markup, 2) if recommended_price is not None else None,
        "gross_margin_percent": round(gross_margin, 2) if recommended_price is not None else None,
        "expected_margin": round(markup, 2) if recommended_price is not None else None,
        "margin_headroom": round(markup - float(owned.minimum_profit_margin or 0), 2) if recommended_price is not None else None,
        "confidence": confidence, "confidence_label": confidence_label,
        "competitor_count": offer_count,
        "reasoning_summary": summary,
        "competitor_summary": (f"Median of {offer_count} validated comparable offer(s) across {marketplace_count} marketplace(s)."
                               if offer_count else "No validated comparable offers; no market price statistics are available."),
        "warnings": warning_list,
        "evidence_type": evidence_type,
        "pricing_method": pricing_method,
        "estimation_status": "available" if local_estimate else ("unavailable" if estimation_error else "not_needed"),
        "local_ai_estimate": local_estimate,
        "minimum_viable_price": round(minimum_display, 2),
        "exclusions": exclusions,
    }
    return {"strategy": strategy, "evidence_type": evidence_type,
            "workflow": state.get("workflow", []) + [{"step": "STRATEGIST", "status": "COMPLETED"}]}


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
    if not state.get("competitors") and evidence_type not in {"LLM_ESTIMATE", "COST_ESTIMATE", "PROVISIONAL"}:
        reasons.append("No competitor evidence or usable estimate was available.")
    try:
        confidence = min(1.0, max(0.0, float(strategy.get("confidence", 0.4))))
    except (TypeError, ValueError):
        confidence = 0.35
    confidence_caps = {"LIVE": 0.90, "STALE_FALLBACK": 0.55, "PROVISIONAL": 0.40,
                       "COST_ESTIMATE": 0.20, "LLM_ESTIMATE": 0.50, "NO_EVIDENCE": 0.0}
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
