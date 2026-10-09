from datetime import datetime, timedelta, timezone
import json
import asyncio
import logging
import re
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi.responses import StreamingResponse

from app.models.retailer_product import RetailerProduct
from app.models.recommendation import Recommendation
from app.models.competitor_price_history import CompetitorPriceHistory
from app.models.competitor_product import CompetitorProduct
from app.models.user import User
from app.models.chat import ChatConversation, ChatMessage
from app.services.auth_service import get_current_user, get_db
from app.services.ai.chroma_service import retrieve_chatbot_context, retrieve_business_knowledge
from app.services.llm_service import generate_structured
from app.config import OLLAMA_TIMEOUT
from app.services.pricing_workflow import analyze_retailer_product
from app.services.market_analysis_service import build_market_evidence, classify_market_question
from app.utils.quantity import normalize_quantity, price_at_target_pack

router = APIRouter()
logger = logging.getLogger(__name__)
_active_workflows = set()
_active_workflows_lock = threading.Lock()


def _business_reasoning(db: Session, product, recommendation, competitors):
    """Return concise margin and compatible-market explanations for the retailer."""
    points = []
    evidence = recommendation.evidence_type
    usable_competitors = competitors if evidence in {"LIVE", "STALE_FALLBACK"} else []
    compared = []
    raw_without_quantity = []
    target = normalize_quantity(product.quantity_value, product.quantity_unit) if product.quantity_value and product.quantity_unit else None
    if target:
        for competitor in usable_competitors:
            equivalent_price = price_at_target_pack(float(competitor.price), product.quantity_value, product.quantity_unit,
                competitor.quantity_value, competitor.quantity_unit, competitor.pack_count,
                competitor.total_quantity, competitor.total_quantity_unit)
            if equivalent_price is not None:
                compared.append((competitor, equivalent_price))
            elif not (competitor.total_quantity or competitor.quantity_value) or not (competitor.total_quantity_unit or competitor.quantity_unit):
                raw_without_quantity.append(competitor)
        prices = [equivalent for _, equivalent in compared]
    else:
        compared = [(competitor, float(competitor.price)) for competitor in usable_competitors
                    if competitor.price and float(competitor.price) > 0]
        prices = [price for _, price in compared]

    from app.services.pricing_service import minimum_viable_selling_price
    floor = minimum_viable_selling_price(product.cost_price, product.minimum_profit_margin)
    rec_price = float(recommendation.recommended_price)
    cost = float(product.cost_price or 0)
    actual_margin = (rec_price - cost) / cost * 100 if cost > 0 else 0.0
    margin_headroom = actual_margin - float(product.minimum_profit_margin or 0)
    points.append(
        f"At ₹{rec_price:,.2f}, each pack earns ₹{rec_price - cost:,.2f} gross profit ({actual_margin:.1f}% margin on cost), "
        f"above the ₹{floor:,.2f} minimum profitable price (floor) by ₹{rec_price - floor:,.2f} and {margin_headroom:.1f} margin points."
    )
    if target:
        points.append(f"Comparisons are normalized to your {float(product.quantity_value):g} {product.quantity_unit} pack.")
    if prices:
        low, high = min(prices), max(prices)
        if rec_price < low:
            position = "below"
        elif rec_price > high:
            position = "above"
        elif abs(low - high) < 0.01:
            position = "aligned with"
        else:
            position = "within"
        basis = f"per {float(product.quantity_value):g} {product.quantity_unit}" if target else "at listed pack sizes"
        points.append(f"Comparable listings ({len(prices)} compatible) range from ₹{low:,.2f}–₹{high:,.2f} {basis}; your price is {position} that range.")
        rated = [(c, equivalent) for c, equivalent in compared if c.rating is not None and c.price and float(c.price) > 0]
        near = [(c, equivalent) for c, equivalent in rated if abs(equivalent - rec_price) <= max(20, rec_price * .15)]
        if near:
            avg_rating = sum(float(c.rating) for c, _ in near) / len(near)
            points.append(f"Comparable options near this price average {avg_rating:.1f} stars across {len(near)} rated listing{'' if len(near) == 1 else 's'}.")
        elif rated:
            best, equivalent = max(rated, key=lambda pair: float(pair[0].rating))
            points.append(f"A highly rated comparable option is ₹{equivalent:,.2f} per target pack ({float(best.rating):.1f} stars).")

        # Only infer direction from repeated, recent historical observations
        # for at least two different competitor listings.
        ids = [c.id for c, _ in compared]
        history = (db.query(CompetitorPriceHistory)
                   .filter(CompetitorPriceHistory.competitor_product_id.in_(ids))
                   .filter(CompetitorPriceHistory.scraped_at >= datetime.now(timezone.utc) - timedelta(days=30))
                   .order_by(CompetitorPriceHistory.scraped_at.asc()).all()) if ids else []
        by_competitor = {}
        for observation in history:
            by_competitor.setdefault(observation.competitor_product_id, []).append(float(observation.price))
        changes = [(values[-1] - values[0]) / values[0] for values in by_competitor.values()
                   if len(values) >= 2 and values[0] > 0]
        if len(changes) >= 2:
            movement = sum(changes) / len(changes)
            if movement >= .03:
                points.append("Similar products have generally moved up in price recently.")
            elif movement <= -.03:
                points.append("Similar products have generally moved down in price recently.")
            else:
                points.append("Similar products have stayed at broadly similar prices recently.")
    elif evidence == "LLM_ESTIMATE":
        pack = f" {product.quantity_value:g} {product.quantity_unit}" if product.quantity_value and product.quantity_unit else ""
        points.append(f"This is an estimate, not a market-data recommendation; it uses your{pack} {product.category or 'product'}, supplied details, and cost of ₹{product.cost_price:,.2f}.")
    if raw_without_quantity and len(points) < 4:
        points.append(f"{len(raw_without_quantity)} listing(s) had no stated quantity; their raw prices were excluded from equivalent-pack comparisons.")
    if len(points) < 4 and product.stock_quantity is not None:
        points.append(f"Your inventory is {int(product.stock_quantity)} pack(s); sales velocity was unavailable to infer demand.")
    return points[:4]


def _recommendation_question(question: str) -> bool:
    text = question.casefold()
    return any(phrase in text for phrase in (
        "recommended price", "what price should", "what should i charge", "price should i sell",
        "how much should i sell", "what price did you recommend", "my recommendation",
    ))


def _recommendation_explanation_question(question: str, history: str = "") -> bool:
    text = f"{history} {question}".casefold()
    return any(phrase in text for phrase in ("why this price", "why did you recommend", "why was this recommended", "why?") )


def _explicit_repricing_request(question: str) -> bool:
    text = " ".join(question.casefold().split())
    explicit_action = any(phrase in text for phrase in ("recalculate", "reprice", "run a new pricing", "run new pricing", "new price analysis"))
    return explicit_action and any(term in text for term in ("price", "pricing", "market"))


def _portfolio_question(question: str) -> bool:
    text = " ".join(question.casefold().split())
    refers_to_multiple = any(term in text for term in ("my products", "product portfolio", "across my products", "all my products"))
    asks_for_action = any(term in text for term in ("which", "review", "priorit", "organize", "attention", "promotion", "promote", "pricing strategy"))
    return refers_to_multiple or ("products" in text and asks_for_action)


def _best_seller_question(question: str) -> bool:
    text = question.casefold()
    return any(term in text for term in ("best-selling", "best selling", "best seller", "top-selling", "top selling", "fastest-selling", "fastest selling"))


def _numerical_forecast_question(question: str) -> bool:
    text = " ".join(question.casefold().split())
    asks_quantity = any(term in text for term in ("how many", "how much demand", "units will", "packs will", "expected to sell", "predict sales"))
    asks_period = any(term in text for term in ("forecast", "next week", "next month", "next quarter", "this month", "this week", "demand next"))
    return (asks_quantity and asks_period) or "forecast my sales" in text or "forecast demand for" in text


def _retailer_portfolio_context(db: Session, user_id: int) -> str:
    """Build a bounded, owner-scoped snapshot without implying sales history."""
    products = (db.query(RetailerProduct).filter_by(user_id=user_id)
                .order_by(RetailerProduct.updated_at.desc(), RetailerProduct.id.desc()).limit(40).all())
    if not products:
        return "No retailer products are currently recorded for this account."
    product_ids = [item.id for item in products]
    recommendation_rows = (db.query(Recommendation).filter(Recommendation.retailer_product_id.in_(product_ids))
                           .order_by(Recommendation.id.desc()).all())
    latest = {}
    for row in recommendation_rows:
        latest.setdefault(row.retailer_product_id, row)
    lines = []
    for item in products:
        recommendation = latest.get(item.id)
        pack = f"{item.quantity_value:g} {item.quantity_unit}" if item.quantity_value and item.quantity_unit else "not recorded"
        saved_price = f"₹{recommendation.recommended_price:,.2f}" if recommendation else "no saved recommendation"
        lines.append(
            f"{item.product_name}; category {item.category or 'not recorded'}; pack {pack}; "
            f"cost ₹{item.cost_price:,.2f}; stock {item.stock_quantity} packs; "
            f"configured minimum profit requirement {item.minimum_profit_margin:.1f}%; recommendation {saved_price}."
        )
    return "Owner's stored product snapshots (not sales rankings):\n" + "\n".join(lines) + "\nNo sales-volume history is available in this context."


def _currency_values(text: str) -> set[float]:
    values = set()
    for value in re.findall(r"(?:₹|\bRs\.?\s*)([\d,]+(?:\.\d{1,2})?)", text, re.I):
        try:
            values.add(round(float(value.replace(",", "")), 2))
        except ValueError:
            pass
    return values


def _unverified_market_amount(answer: str, evidence: str, recommendation) -> bool:
    allowed = _currency_values(evidence)
    if recommendation is not None:
        allowed.update(round(float(value), 2) for value in (
            recommendation.recommended_price, recommendation.recommended_price_min,
            recommendation.recommended_price_max,
        ) if value is not None)
    return any(value not in allowed for value in _currency_values(answer))


class ChatRequest(BaseModel):
    retailer_product_id: int | None = None
    conversation_id: int | None = None
    question: str = Field(min_length=2, max_length=1200)


class ConversationRequest(BaseModel):
    retailer_product_id: int | None = None


def _conversation_payload(conversation, db):
    messages = (db.query(ChatMessage).filter_by(conversation_id=conversation.id)
                .order_by(ChatMessage.id.asc()).all())
    return {"id": conversation.id, "product_id": conversation.product_id,
            "messages": [{"id": m.id, "role": m.role, "content": m.content,
                          "created_at": m.created_at.isoformat() if m.created_at else None} for m in messages]}


@router.post("/chat/conversations")
def create_chat_conversation(request: ConversationRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    product_id = request.retailer_product_id
    if product_id is not None and not db.query(RetailerProduct).filter_by(id=product_id, user_id=current_user.id).first():
        raise HTTPException(status_code=404, detail="Product not found")
    conversation = ChatConversation(retailer_id=current_user.id, product_id=product_id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return _conversation_payload(conversation, db)


@router.get("/chat/conversations/latest")
def latest_chat_conversation(retailer_product_id: int | None = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if retailer_product_id is not None and not db.query(RetailerProduct).filter_by(id=retailer_product_id, user_id=current_user.id).first():
        raise HTTPException(status_code=404, detail="Product not found")
    query = db.query(ChatConversation).filter_by(retailer_id=current_user.id)
    if retailer_product_id is not None:
        query = query.filter_by(product_id=retailer_product_id)
    conversation = query.order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc()).first()
    if conversation is None:
        conversation = ChatConversation(retailer_id=current_user.id, product_id=retailer_product_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return _conversation_payload(conversation, db)


@router.get("/chat/conversations/{conversation_id}")
def get_chat_conversation(conversation_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    conversation = db.query(ChatConversation).filter_by(id=conversation_id, retailer_id=current_user.id).first()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _conversation_payload(conversation, db)


def _analysis_response(db: Session, owned: RetailerProduct, result: dict):
    recommendation = result.get("recommendation")
    competitors = result.get("competitors", [])
    marketplace_products = {name: [] for name in ("Amazon", "Flipkart", "Myntra", "Meesho")}
    for item in competitors:
        equivalent_price = None
        if owned.quantity_value and owned.quantity_unit:
            equivalent_price = price_at_target_pack(
                float(item.price), owned.quantity_value, owned.quantity_unit,
                item.quantity_value, item.quantity_unit, item.pack_count,
                item.total_quantity, item.total_quantity_unit,
            )
        quantity_text = (
            f"{float(item.total_quantity):g} {item.total_quantity_unit}"
            if item.total_quantity and item.total_quantity_unit else
            (f"{float(item.quantity_value):g} {item.quantity_unit}" if item.quantity_value and item.quantity_unit else "Not specified")
        )
        marketplace_products.setdefault(item.platform_name, []).append({
            "product_name": item.product_name,
            "price": item.price,
            "rating": item.rating,
            "rating_display": f"{float(item.rating):.1f}" if item.rating is not None else "Not rated",
            "product_url": item.product_url,
            "product_details": item.product_details,
            "quantity_value": item.quantity_value,
            "quantity_unit": item.quantity_unit,
            "quantity_display": quantity_text,
            "pack_count": item.pack_count,
            "total_quantity": item.total_quantity,
            "total_quantity_unit": item.total_quantity_unit,
            "equivalent_price": round(equivalent_price, 2) if equivalent_price is not None else None,
            "equivalent_price_display": (f"₹{equivalent_price:,.2f} per {float(owned.quantity_value):g} {owned.quantity_unit}"
                                         if equivalent_price is not None else "Not available"),
            "availability_display": "Not reported" if item.availability is None else ("In stock" if item.availability else "Out of stock"),
            "product_details_display": item.product_details or "Not available",
        })
    strategy = result.get("strategy") or {}
    from app.services.pricing_service import minimum_viable_selling_price
    minimum_price = minimum_viable_selling_price(owned.cost_price, owned.minimum_profit_margin)
    market_low = strategy.get("market_price_range_low")
    market_high = strategy.get("market_price_range_high")
    market_range_display = (f"₹{market_low:,.2f}–₹{market_high:,.2f} per {float(owned.quantity_value):g} {owned.quantity_unit}"
                            if market_low is not None and market_high is not None and owned.quantity_value and owned.quantity_unit
                            else "Not available")
    return {
        "evidence_type": result.get("evidence_type", "NO_EVIDENCE"),
        "freshness_status": result.get("freshness_status", "NO_EVIDENCE"),
        "marketplace_statuses": result.get("marketplace_statuses", {}),
        "marketplace_products": marketplace_products,
        "compliance": result.get("compliance", {"accepted": False, "reasons": [result.get("error", "Workflow stopped.")]}),
        "error": result.get("identity_error") or result.get("error"),
        "warnings": (result.get("strategy") or {}).get("warnings", result.get("warnings", [])),
        "competitor_summary": (result.get("strategy") or {}).get("competitor_summary"),
        "recommendation": ({"id": recommendation.id, "recommended_price": recommendation.recommended_price,
            "expected_profit": recommendation.expected_profit, "profit_percentage": recommendation.profit_percentage,
            "reasoning_summary": recommendation.reasoning, "reasoning": _business_reasoning(db, owned, recommendation, competitors), "confidence": recommendation.confidence_score,
            "evidence_type": recommendation.evidence_type,
            "basis": "MARKET_DATA" if recommendation.evidence_type in {"LIVE", "STALE_FALLBACK"} else "ESTIMATE",
            "retailer_quantity_display": (f"{float(owned.quantity_value):g} {owned.quantity_unit}" if owned.quantity_value and owned.quantity_unit else "Not specified"),
            "cost_price": owned.cost_price,
            "minimum_profit_margin": owned.minimum_profit_margin,
            "minimum_viable_price": minimum_price,
            "actual_margin_percent": recommendation.profit_percentage,
            "margin_headroom_percent": recommendation.profit_percentage - owned.minimum_profit_margin,
            "market_price_range_display": market_range_display,
            "accepted_by_user": recommendation.accepted_by_user,
            "price_range_low": (result.get("strategy") or {}).get("price_range_low"),
            "price_range_high": (result.get("strategy") or {}).get("price_range_high"),
            "recommended_price_min": recommendation.recommended_price_min,
            "recommended_price_max": recommendation.recommended_price_max}
            if recommendation else None),
    }


@router.post("/workflow/{retailer_product_id}/analyze")
def analyze(retailer_product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owned = db.query(RetailerProduct).filter_by(id=retailer_product_id, user_id=current_user.id).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        result = analyze_retailer_product(db, retailer_product_id)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Price analysis could not be completed. Please try again.")
    if result.get("identity_error"):
        raise HTTPException(status_code=503, detail=result["identity_error"])
    if result.get("error"):
        if result["error"] == "Price analysis could not be completed right now. Please try again.":
            raise HTTPException(status_code=503, detail=result["error"])
        raise HTTPException(status_code=500, detail="We couldn't complete the product analysis. Please try again.")
    return _analysis_response(db, owned, result)


@router.post("/workflow/{retailer_product_id}/analyze/stream")
def analyze_stream(retailer_product_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owned = db.query(RetailerProduct).filter_by(id=retailer_product_id, user_id=current_user.id).first()
    if not owned:
        raise HTTPException(status_code=404, detail="Product not found")
    from app.database import SessionLocal

    events = queue.Queue()
    retailer_id = current_user.id
    cancel_event = threading.Event()
    with _active_workflows_lock:
        if retailer_product_id in _active_workflows:
            raise HTTPException(status_code=409, detail="A pricing analysis is already running for this product.")
        _active_workflows.add(retailer_product_id)

    def run_graph():
        worker_db = None
        try:
            worker_db = SessionLocal()
            worker_owned = worker_db.query(RetailerProduct).filter_by(id=retailer_product_id, user_id=retailer_id).first()
            if not worker_owned:
                events.put({"type": "error", "message": "Product not found."})
            else:
                result = analyze_retailer_product(
                    worker_db, retailer_product_id,
                    progress_callback=lambda event: events.put({"type": "progress", **event}),
                    cancel_event=cancel_event,
                )
                if result.get("error"):
                    events.put({"type": "error", "message": result.get("identity_error") or result.get("error") or "Price analysis could not be completed right now. Please try again."})
                else:
                    events.put({"type": "result", "result": _analysis_response(worker_db, worker_owned, result)})
        except Exception as exc:
            if worker_db is not None:
                worker_db.rollback()
            logger.error("Pricing workflow failed: error_type=%s", type(exc).__name__)
            events.put({"type": "error", "message": "We couldn't complete the pricing analysis. Please try again."})
        finally:
            if worker_db is not None:
                worker_db.close()
            with _active_workflows_lock:
                _active_workflows.discard(retailer_product_id)
            events.put(None)

    threading.Thread(target=run_graph, daemon=True).start()

    async def stream_events():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.to_thread(events.get, True, 15)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, default=str)}\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(stream_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    product = None
    if request.retailer_product_id is not None:
        product = db.query(RetailerProduct).filter_by(id=request.retailer_product_id, user_id=current_user.id).first()
    if request.retailer_product_id is not None and not product:
        raise HTTPException(status_code=404, detail="Product not found")
    conversation = None
    if request.conversation_id is not None:
        conversation = db.query(ChatConversation).filter_by(id=request.conversation_id, retailer_id=current_user.id).first()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conversation.product_id is not None and (product is None or product.id != conversation.product_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conversation.product_id is None and product is not None:
            conversation.product_id = product.id
    else:
        conversation = ChatConversation(retailer_id=current_user.id, product_id=product.id if product else None)
        db.add(conversation)
        db.flush()
    recommendation = (db.query(Recommendation).filter_by(retailer_product_id=product.id)
                      .order_by(Recommendation.id.desc()).first()) if product else None
    current_message = ChatMessage(conversation_id=conversation.id, role="user", content=request.question)
    db.add(current_message)
    db.flush()
    history = (db.query(ChatMessage).filter_by(conversation_id=conversation.id)
               .filter(ChatMessage.id != current_message.id)
               .order_by(ChatMessage.id.desc()).limit(16).all())
    history.reverse()
    history_text = "\n".join(f"{m.role.title()}: {m.content}" for m in history)
    # Prior market topics help interpret conversational follow-ups, but must not
    # turn a later recommendation explanation into a market-trend response.
    market_intent = classify_market_question(request.question, "")
    market_context = ""
    if market_intent:
        try:
            if market_intent == "MARKET_ANALYSIS" and product and not _explicit_repricing_request(request.question):
                from app.services.competitor_service import get_competitor_freshness
                if get_competitor_freshness(db, product.catalog_product_id)["status"] != "FRESH":
                    # Reuse PricePulse's marketplace collection pipeline for current requests.
                    # Scrape failures are isolated by the service and never become invented facts.
                    from app.models.product_catalog import ProductCatalog
                    from app.services.scraping_service import scrape_product
                    catalog = db.query(ProductCatalog).filter_by(id=product.catalog_product_id).first()
                    if catalog:
                        scrape_product(db, catalog)
            market_context = build_market_evidence(db, product.catalog_product_id if product else None, market_intent,
                                                   question=request.question)
        except Exception:
            logger.warning("Marketplace evidence lookup failed; continuing without current or historical claims", exc_info=True)
            market_context = "Marketplace evidence could not be retrieved. Do not make current or historical market claims."
    repricing_payload = None
    answer_override = None
    if product and _explicit_repricing_request(request.question):
        try:
            pricing_result = analyze_retailer_product(db, product.id)
            recommendation = pricing_result.get("recommendation")
            if recommendation is not None:
                product = db.query(RetailerProduct).filter_by(id=product.id, user_id=current_user.id).first()
                repricing_payload = _analysis_response(db, product, pricing_result)
                answer_override = (
                    f"Pricing analysis is complete. Your new saved recommendation for {product.product_name} is "
                    f"₹{recommendation.recommended_price:,.0f}. Suggested range: "
                    f"₹{recommendation.recommended_price_min:,.0f}–₹{recommendation.recommended_price_max:,.0f}."
                )
            else:
                answer_override = "I couldn't complete the new pricing analysis, so your saved recommendation has not been changed. Please try again."
        except Exception:
            db.rollback()
            logger.exception("Explicit repricing workflow failed for retailer_product_id=%s", product.id)
            answer_override = "I couldn't complete the new pricing analysis, so your saved recommendation has not been changed. Please try again."
    context = ""
    competitor_context = ""
    business_context = ""
    portfolio_context = ""
    try:
        business_context = retrieve_business_knowledge("\n".join(filter(None, [request.question, history_text]))) or ""
    except Exception:
        logger.exception("Business knowledge retrieval failed; continuing with Qwen and PricePulse context")
    if product is None and _portfolio_question(request.question):
        portfolio_context = _retailer_portfolio_context(db, current_user.id)
    if product:
        competitor_rows = db.query(CompetitorProduct).filter_by(catalog_product_id=product.catalog_product_id).all()
        competitor_lines = []
        for item in competitor_rows:
            quantity_label = f"{item.quantity_value:g} {item.quantity_unit}" if item.quantity_value and item.quantity_unit else "quantity not recorded"
            observed_value = item.total_quantity if item.total_quantity is not None else item.quantity_value
            observed_unit = item.total_quantity_unit or item.quantity_unit
            if item.total_quantity is None and observed_value is not None:
                observed_value *= item.pack_count or 1
            normalized = normalize_quantity(observed_value, observed_unit) if observed_value and observed_unit else None
            unit_label = (f"; unit price ₹{float(item.price) / normalized[0]:,.4f} per {'g' if normalized[1] == 'mass' else 'ml'}"
                          if normalized and normalized[0] > 0 else "")
            equivalent = price_at_target_pack(float(item.price), product.quantity_value, product.quantity_unit,
                item.quantity_value, item.quantity_unit, item.pack_count, item.total_quantity, item.total_quantity_unit) \
                if product.quantity_value and product.quantity_unit else None
            equivalent_label = f"; target-pack equivalent ₹{equivalent:,.2f}" if equivalent is not None else ""
            competitor_lines.append(
                f"{item.product_name}; {item.platform_name}; price ₹{float(item.price):,.2f}; pack {quantity_label}"
                f"{unit_label}{equivalent_label}; availability {'in stock' if item.availability else 'unavailable/unknown'}; "
                f"observed_at {item.scraped_at.isoformat() if item.scraped_at else 'unknown'}."
            )
        competitor_context = "\n".join(competitor_lines[:20])
        rag_query = "\n".join(filter(None, [request.question, product.product_name, product.category or "", product.product_details or ""]))
        try:
            context = retrieve_chatbot_context(rag_query, product.catalog_product_id, n_results=20, retailer_product_id=product.id, user_id=current_user.id) or ""
        except Exception:
            logger.warning("Chat pricing context retrieval was unavailable", exc_info=True)
    recommendation_context = ""
    if recommendation:
        range_text = (f"Suggested price range: ₹{recommendation.recommended_price_min:,.2f}–₹{recommendation.recommended_price_max:,.2f}."
                      if recommendation.recommended_price_min is not None and recommendation.recommended_price_max is not None else "")
        recommendation_context = (
            f"Current persisted recommendation for {product.product_name}: recommended selling price ₹{recommendation.recommended_price:,.2f}. "
            f"{range_text} Expected profit margin: {recommendation.profit_percentage:.1f}%. "
            f"Basis: {'MARKET_DATA' if recommendation.evidence_type in {'LIVE', 'STALE_FALLBACK'} else 'ESTIMATE'}. "
            f"Saved reasoning: {recommendation.reasoning or 'No persisted reasoning was saved.'} "
            f"Minimum profit requirement: {product.minimum_profit_margin:.1f}%. Product cost: ₹{product.cost_price:,.2f}."
        )
    product_context = (
        f"Current product: {product.product_name}; category: {product.category or 'not provided'}; "
        f"brand: {product.brand or 'not provided'}; details: {product.product_details or 'not provided'}; "
        f"commercial pack quantity: {f'{product.quantity_value:g} {product.quantity_unit}' if product.quantity_value and product.quantity_unit else 'not provided'}; "
        f"cost: ₹{product.cost_price:.2f}; stock quantity: {product.stock_quantity} packs; "
        f"minimum profit requirement: {product.minimum_profit_margin:.1f}%."
    ) if product else "No product is selected."
    prompt = (
        "You are PricePulse Assistant, a business and pricing advisor for small e-commerce retailers. Help with pricing, profitability, competition, inventory, promotions, marketplace strategy, product positioning, and business decisions. "
        "Use the supplied conversation history to understand follow-ups. Use PricePulse data and curated business knowledge when supplied. "
        "Do not invent prices, competitors, ratings, trends, historical prices, averages, reviews, sales volume, market share, conversion, customer demographics, demand, turnover, or financial evidence. Clearly distinguish actual retailer data, curated business knowledge, and your labeled inference. "
        "PricePulse has stock quantities but no historical unit-sales or customer-segment history in this chat. Never infer sales velocity, best sellers, reorder demand, inventory turnover, customer demographics, or a numerical forecast from stock-on-hand. State when the required data is unavailable, then give a practical general method. "
        "A saved PostgreSQL recommendation is the only source of the user's recommended price and range. Never independently recalculate or replace it in chat. Mention it only when relevant. Market and general business answers are informational and do not change saved pricing. "
        "For financial safety questions, use actual product cost, configured minimum requirement, and saved/current selling price when present; if an input is missing, say what is missing. Label product-cost calculations as excluding any unprovided channel, shipping, tax, or fulfillment costs. "
        "Compare competitor pack prices by compatible unit and pack size; never rank unlike raw checkout prices as better value. If units are incompatible or quantities are missing, explain the limitation. "
        "Structure planning replies with a direct answer, key factors, and practical next steps. Give understandable recommendations without a blind yes/no. Do not expose internal implementation details or hidden reasoning.\n"
        "RECENT CONVERSATION (oldest first):\n" + (history_text or "No earlier messages.")
        + "\nCURRENT PRODUCT:\n" + (product_context if product else "No product is selected.")
        + "\nPERSISTED RECOMMENDATION:\n" + (recommendation_context or "No persisted recommendation exists.")
        + "\nOWNER PRODUCT PORTFOLIO (shown only for portfolio questions):\n" + (portfolio_context or "No portfolio lookup was needed.")
        + "\nSTORED COMPETITOR OBSERVATIONS (compare quantities only within compatible dimensions):\n"
        + (competitor_context or "No stored competitor observations are available.")
        + "\nCURATED BUSINESS KNOWLEDGE:\n" + (business_context or "No relevant curated business document was retrieved; use general business knowledge as appropriate.")
        + "\nPRICEPULSE PRICING CONTEXT:\n" + (context or "No relevant stored pricing information was retrieved.")
        + "\nOBSERVED MARKETPLACE EVIDENCE (timestamps and sample limits are material):\n"
        + (market_context or "No market evidence lookup was needed. Never infer current or historical market facts without retrieved observations.")
        + "\nFor time-sensitive market questions, distinguish fresh observations from history. If no fresh data exists, say you can explain business implications but lack enough current evidence for a reliable real-time claim. Do not label a small sample as a market-wide trend. Use the business knowledge only to explain implications, not as evidence of current conditions.\n"
        + "\nCURRENT QUESTION:\n" + request.question
    )
    recommendation_question = _recommendation_question(request.question)
    explanation_question = _recommendation_explanation_question(request.question, history_text)
    if answer_override is None and _best_seller_question(request.question):
        answer_override = ("I don't have sales-volume history to identify a best-selling product reliably. "
                           "Compare dated units sold over the same period, account for stockouts and promotions, "
                           "and review contribution as well as units before prioritizing a product.")
    if answer_override is None and _numerical_forecast_question(request.question):
        item = f" for {product.product_name}" if product else ""
        answer_override = (f"I don't have enough historical sales-volume data to produce a reliable numerical forecast{item}. "
                           "Stock on hand is not sales velocity. For a useful estimate, provide dated unit sales, "
                           "stockout periods, price or promotion changes, and supplier lead times; until then, "
                           "use a clearly labeled planning assumption and update it as observations accumulate.")
    if answer_override is None and product and recommendation is None and recommendation_question:
        answer_override = "There is no saved price recommendation for this product yet. Run Analyze pricing to create one; I won't invent a price in chat."
    elif answer_override is None and product and recommendation is None and explanation_question:
        answer_override = "There is no saved recommendation to explain yet. Run Analyze pricing first."
    if answer_override is None and recommendation is not None and product and recommendation_question:
        answer_override = (
            f"Your saved recommendation for {product.product_name} is ₹{recommendation.recommended_price:,.0f}. "
            + (f"The suggested range is ₹{recommendation.recommended_price_min:,.0f}–₹{recommendation.recommended_price_max:,.0f}. "
               if recommendation.recommended_price_min is not None and recommendation.recommended_price_max is not None else "")
            + f"Basis: {'market evidence' if recommendation.evidence_type in {'LIVE', 'STALE_FALLBACK'} else 'product and business estimate'}."
        )
    elif answer_override is None and recommendation is not None and product and explanation_question:
        competitors = db.query(CompetitorProduct).filter_by(catalog_product_id=product.catalog_product_id).all()
        evidence_points = _business_reasoning(db, product, recommendation, competitors)
        answer_override = (f"I recommended ₹{recommendation.recommended_price:,.0f} for {product.product_name}. "
            + (recommendation.reasoning + " " if recommendation.reasoning else "")
            + " ".join(evidence_points))
    try:
        response = ({"success": True, "data": {"answer": answer_override}} if answer_override is not None else generate_structured(
            prompt,
            schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
            system_prompt="You are PricePulse Assistant. Return one valid JSON object with a concise string field named answer.",
            timeout=OLLAMA_TIMEOUT,
            operation="general_chat",
        ))
    except Exception:
        logger.exception("Chat response generation raised an unexpected exception")
        response = {"success": False, "data": None}
    answer = (response.get("data") or {}).get("answer") if response.get("success") else None
    if not isinstance(answer, str) or not answer.strip():
        logger.error("Chat response contract failed: success=%s error=%s data_keys=%s",
                     response.get("success"), response.get("error"),
                     sorted((response.get("data") or {}).keys()) if isinstance(response.get("data"), dict) else [])
    internal_terms = ("couldn't get a grounded answer", "could not get a grounded answer", "chromadb", "embedding", "langgraph", "strategist", "compliance", "scout", "qwen", "ollama", "chain-of-thought")
    answer_text = answer.strip() if isinstance(answer, str) else ""
    if market_intent and answer_text:
        mentions_recommendation = "recommend" in answer_text.casefold()
        saved_values = ({round(float(recommendation.recommended_price), 2)} if recommendation else set())
        if (_unverified_market_amount(answer_text, market_context, recommendation)
                or (mentions_recommendation and recommendation is not None
                    and not saved_values.issubset(_currency_values(answer_text)))):
            if recommendation is not None:
                if "No marketplace listing is fresh enough" in market_context:
                    trend_limit = "No fresh marketplace evidence is available, so I can't reliably state the current market trend."
                else:
                    trend_limit = "I can't make a reliable market-wide trend claim from the available marketplace sample."
                answer = (trend_limit + f" Your saved recommendation for {product.product_name} remains "
                          f"₹{recommendation.recommended_price:,.0f}. This chat has not changed it.")
                answer_text = answer
            else:
                answer = "I don't have enough verified marketplace evidence to state a current trend. This chat does not change any saved recommendation."
                answer_text = answer
    if not answer_text or any(term in answer_text.casefold() for term in internal_terms):
        price_follow_up_terms = ("price", "pricing", "recommend", "range", "cost", "profit", "margin", "competitor", "increase", "decrease")
        current_and_prior_text = (request.question + " " + history_text).casefold()
        pricing_context_active = (any(term in current_and_prior_text for term in price_follow_up_terms)
                                  or bool(product and product.product_name.casefold() in current_and_prior_text))
        if recommendation and pricing_context_active:
            from app.services.pricing_service import minimum_viable_selling_price
            floor = minimum_viable_selling_price(product.cost_price, product.minimum_profit_margin)
            answer = (
                f"{product.product_name} has a recommended selling price of ₹{recommendation.recommended_price:,.0f}. "
                + (f"The suggested range is ₹{recommendation.recommended_price_min:,.0f}–₹{recommendation.recommended_price_max:,.0f}. "
                   if recommendation.recommended_price_min is not None and recommendation.recommended_price_max is not None else "")
                + f"Your minimum profitable price is ₹{floor:,.0f}, based on the saved cost and margin requirement."
            )
        else:
            logger.warning("Chat response unavailable to conversation_id=%s", conversation.id)
            answer = "The assistant is temporarily unavailable. Please try again shortly."
    elif recommendation and (recommendation_question or explanation_question) and f"{recommendation.recommended_price:,.0f}" not in answer_text:
        persisted_summary = (
            f"{product.product_name} has a recommended selling price of ₹{recommendation.recommended_price:,.0f}. "
            + (f"The suggested range is ₹{recommendation.recommended_price_min:,.0f}–₹{recommendation.recommended_price_max:,.0f}. "
               if recommendation.recommended_price_min is not None and recommendation.recommended_price_max is not None else "")
        )
        answer = persisted_summary + answer_text
    answer = answer.strip()
    db.add(ChatMessage(conversation_id=conversation.id, role="assistant", content=answer))
    conversation.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Chat message persistence failed for conversation_id=%s", conversation.id)
        raise HTTPException(status_code=503, detail="I couldn't save the conversation right now. Please try again.") from exc
    source_labels = ["GENERAL_QWEN"] if response.get("success") and answer_text else []
    if history:
        source_labels.append("CONVERSATION_HISTORY")
    if product:
        source_labels.append("PRODUCT_CONTEXT")
    if recommendation:
        source_labels.append("RECOMMENDATION_CONTEXT")
    if business_context:
        source_labels.append("BUSINESS_KNOWLEDGE")
    if context:
        source_labels.append("PRICE_RAG")
        if "competitor product" in context.casefold():
            source_labels.append("COMPETITOR_CONTEXT")
    if market_context:
        source_labels.append("MARKETPLACE_EVIDENCE")
    logger.info("Chat response context sources: %s", ",".join(source_labels))
    return {"conversation_id": conversation.id, "answer": answer,
            "grounded": bool(history or context or business_context or recommendation or product or market_context),
            "repricing": repricing_payload}
