from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.chat import ChatConversation, ChatMessage
from app.models.competitor_product import CompetitorProduct
from app.models.competitor_price_history import CompetitorPriceHistory
from app.models.product_catalog import ProductCatalog
from app.models.retailer_product import RetailerProduct
from app.models.recommendation import Recommendation
from app.models.user import User
from app.routes.workflow import ChatRequest, ConversationRequest, chat, create_chat_conversation, get_chat_conversation, latest_chat_conversation


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def avoid_writing_to_live_knowledge_store(monkeypatch):
    monkeypatch.setattr("app.routes.workflow.retrieve_business_knowledge", lambda _query: "")


def user_and_product(db, suffix):
    user = User(email=f"chat-{suffix}@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.flush()
    catalog = ProductCatalog(name="Gundhal hair oil", category="Hair care", product_details="100 ml")
    db.add(catalog)
    db.flush()
    product = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id, cost_price=100,
                              minimum_profit_margin=20, stock_quantity=5)
    db.add(product)
    db.commit()
    return user, product


def test_chat_persists_messages_and_supplies_bounded_history(db):
    user, product = user_and_product(db, "history")
    conversation = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, user)
    observed_prompts = []

    def answer(prompt, **_kwargs):
        observed_prompts.append(prompt)
        return {"success": True, "data": {"answer": "The answer is grounded in the product context."}}

    with patch("app.routes.workflow.retrieve_chatbot_context", return_value=""), patch("app.routes.workflow.generate_structured", side_effect=answer):
        for text in ("What is gross margin?", "How is it different from markup?", "How does that relate to my product cost?"):
            result = chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"], question=text), db, user)
            assert result["conversation_id"] == conversation["id"]

    messages = db.query(ChatMessage).filter_by(conversation_id=conversation["id"]).order_by(ChatMessage.id).all()
    assert [(item.role, item.content) for item in messages] == [
        ("user", "What is gross margin?"), ("assistant", "The answer is grounded in the product context."),
        ("user", "How is it different from markup?"), ("assistant", "The answer is grounded in the product context."),
        ("user", "How does that relate to my product cost?"), ("assistant", "The answer is grounded in the product context."),
    ]
    assert "User: What is gross margin?" in observed_prompts[1]
    assert "Assistant: The answer is grounded" in observed_prompts[1]
    assert "User: How is it different from markup?" in observed_prompts[2]
    assert "How does that relate to my product cost?" in observed_prompts[2]


def test_recommendation_and_market_followups_never_diverge_from_saved_price(db):
    user, product = user_and_product(db, "canonical-price")
    saved = Recommendation(retailer_product_id=product.id, recommended_price=154,
        recommended_price_min=154, recommended_price_max=161, expected_profit=54,
        profit_percentage=54, reasoning="The recommendation uses comparable evidence and respects the margin floor.",
        evidence_type="LIVE", accepted_by_user=False)
    db.add(saved); db.commit()
    conversation = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, user)
    def inconsistent_market_answer(prompt, **_kwargs):
        assert "recommended selling price ₹154.00" in prompt
        return {"success": True, "data": {"answer": "The current trend suggests your recommended price is ₹23 with a range of ₹21–₹27."}}
    with patch("app.services.competitor_service.get_competitor_freshness", return_value={"status": "FRESH"}), \
         patch("app.routes.workflow.retrieve_chatbot_context", return_value=""), \
         patch("app.routes.workflow.generate_structured", side_effect=inconsistent_market_answer) as generate:
        first = chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"],
                                 question="What is my recommended price?"), db, user)
        trend = chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"],
                                 question="What is the current trend in market?"), db, user)
        why = chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"],
                               question="Why did you recommend that price?"), db, user)
    assert "₹154" in first["answer"] and "₹154–₹161" in first["answer"]
    assert "₹23" not in trend["answer"] and "₹21–₹27" not in trend["answer"]
    assert "₹154" in trend["answer"] and "remains" in trend["answer"]
    assert "₹154" in why["answer"]
    assert "comparable evidence" in why["answer"]
    assert generate.call_count == 1  # Only the market question is passed through Qwen.
    db.refresh(saved)
    assert (saved.recommended_price, saved.recommended_price_min, saved.recommended_price_max) == (154, 154, 161)


def test_only_explicit_repricing_request_starts_new_workflow_and_updates_saved_context(db):
    user, product = user_and_product(db, "explicit-reprice")
    previous = Recommendation(retailer_product_id=product.id, recommended_price=154,
        recommended_price_min=154, recommended_price_max=161, expected_profit=54,
        profit_percentage=54, reasoning="Original saved recommendation.", evidence_type="LIVE", accepted_by_user=False)
    db.add(previous); db.commit()
    calls = []
    def reprice(_db, product_id):
        calls.append(product_id)
        updated = Recommendation(retailer_product_id=product_id, recommended_price=180,
            recommended_price_min=170, recommended_price_max=190, expected_profit=80,
            profit_percentage=80, reasoning="Updated after explicit analysis.", evidence_type="LIVE", accepted_by_user=False)
        db.add(updated); db.commit(); db.refresh(updated)
        return {"recommendation": updated, "competitors": [], "evidence_type": "LIVE",
                "compliance": {"accepted": True, "reasons": []}, "strategy": {}}
    with patch("app.services.competitor_service.get_competitor_freshness", return_value={"status": "FRESH"}), \
         patch("app.routes.workflow.analyze_retailer_product", side_effect=reprice), \
         patch("app.routes.workflow.retrieve_chatbot_context", return_value=""), \
         patch("app.routes.workflow.generate_structured") as generate:
        informational = chat(ChatRequest(retailer_product_id=product.id,
            question="What is the current trend in market?"), db, user)
        assert informational["repricing"] is None
        assert calls == []
        explicit = chat(ChatRequest(retailer_product_id=product.id,
            question="Recalculate my price based on the latest market data."), db, user)
    assert calls == [product.id]
    assert explicit["repricing"]["recommendation"]["recommended_price"] == 180
    assert "₹180" in explicit["answer"]
    generate.assert_called_once()  # Only the ordinary informational question uses Qwen.


def test_conversation_reads_are_tenant_scoped(db):
    owner, product = user_and_product(db, "owner")
    other, _ = user_and_product(db, "other")
    conversation = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, owner)
    with pytest.raises(HTTPException) as error:
        get_chat_conversation(conversation["id"], db, other)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        chat(ChatRequest(conversation_id=conversation["id"], retailer_product_id=product.id, question="Why?"), db, other)
    assert error.value.status_code == 404
    other_conversation = create_chat_conversation(ConversationRequest(), db, other)
    with pytest.raises(HTTPException) as error:
        get_chat_conversation(other_conversation["id"], db, owner)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        chat(ChatRequest(conversation_id=other_conversation["id"], question="What is gross margin?"), db, owner)
    assert error.value.status_code == 404


def test_latest_conversation_without_product_recovers_latest_product_chat(db):
    user, product = user_and_product(db, "latest")
    general = create_chat_conversation(ConversationRequest(), db, user)
    product_chat = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, user)
    db.add(ChatMessage(conversation_id=product_chat["id"], role="user", content="What is my price?"))
    db.commit()
    latest = latest_chat_conversation(None, db, user)
    assert latest["id"] == product_chat["id"]
    assert latest["id"] != general["id"]
    assert latest["product_id"] == product.id


def test_general_question_does_not_require_product_or_rag(db):
    user = User(email="chat-general@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.retrieve_chatbot_context") as retrieve, patch(
        "app.routes.workflow.retrieve_business_knowledge", return_value="Profit & Margin: curated definition and formula."
    ), patch(
        "app.routes.workflow.generate_structured", return_value={"success": True, "data": {"answer": "Gross margin is profit as a share of selling price."}}
    ) as generate:
        result = chat(ChatRequest(conversation_id=conversation["id"], question="Explain gross margin simply."), db, user)
    assert "Gross margin" in result["answer"]
    retrieve.assert_not_called()
    assert "No product is selected." in generate.call_args.args[0]
    assert "curated definition and formula" in generate.call_args.args[0]
    assert generate.call_args.kwargs["schema"]["required"] == ["answer"]
    assert "PricePulse Assistant" in generate.call_args.kwargs["system_prompt"]


def test_basic_hi_returns_qwen_answer_without_product_or_rag(db):
    user = User(email="chat-hi@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.generate_structured", return_value={
        "success": True, "data": {"answer": "Hello! I can help with business and pricing questions."}
    }) as generate:
        result = chat(ChatRequest(conversation_id=conversation["id"], question="Hi"), db, user)
    assert "Hello" in result["answer"]
    assert "temporarily unavailable" not in result["answer"]
    assert "No product is selected." in generate.call_args.args[0]


def test_general_business_question_answers_when_both_rag_sources_are_empty(db):
    user = User(email="chat-no-rag@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.retrieve_business_knowledge", return_value=""), \
         patch("app.routes.workflow.retrieve_chatbot_context") as retrieve_price_rag, \
         patch("app.routes.workflow.generate_structured", return_value={
             "success": True, "data": {"answer": "Gross margin divides gross profit by selling price."}
         }):
        result = chat(ChatRequest(conversation_id=conversation["id"], question="What is gross margin?"), db, user)
    assert "selling price" in result["answer"]
    retrieve_price_rag.assert_not_called()


def test_qwen_failure_returns_controlled_error_instead_of_fabricated_answer(db):
    user = User(email="chat-down@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.generate_structured", return_value={
        "success": False, "data": None, "error": "Ollama timeout"
    }):
        result = chat(ChatRequest(conversation_id=conversation["id"], question="Hi"), db, user)
    assert result["answer"] == "The assistant is temporarily unavailable. Please try again shortly."
    assert [m.role for m in db.query(ChatMessage).filter_by(conversation_id=conversation["id"]).order_by(ChatMessage.id)] == ["user", "assistant"]


def test_unexpected_qwen_response_shape_is_reported_not_silently_dropped(db, caplog):
    user = User(email="chat-shape@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.generate_structured", return_value={
        "success": True, "data": {"response": "Qwen produced a response under the wrong key."}
    }):
        result = chat(ChatRequest(conversation_id=conversation["id"], question="Hi"), db, user)
    assert result["answer"] == "The assistant is temporarily unavailable. Please try again shortly."
    assert "Chat response contract failed" in caplog.text


def test_business_knowledge_retrieval_is_dynamic_and_ignores_greeting_noise():
    from app.services.ai import chroma_service
    hits = [
        {"text": "Profit & Margin: Gross margin is gross profit divided by selling price. Markup divides profit by cost.", "metadata": {"topic": "Profit & Margin"}},
        {"text": "Inventory information can explain a topic but not its definition.", "metadata": {"topic": "Inventory"}},
    ]
    with patch.object(chroma_service, "seed_business_knowledge") as seed, patch.object(
        chroma_service, "retrieve_relevant", return_value=hits
    ) as retrieve:
        result = chroma_service.retrieve_business_knowledge("Explain gross margin")
        greeting_result = chroma_service.retrieve_business_knowledge("Hi")
    assert "gross profit divided by selling price" in result
    assert "Inventory" not in result
    assert greeting_result == ""
    assert seed.call_count == 1
    assert retrieve.call_count == 1


def test_chat_continues_when_business_and_product_rag_both_raise(db):
    user, product = user_and_product(db, "rag-down")
    conversation = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, user)
    with patch("app.routes.workflow.retrieve_business_knowledge", side_effect=RuntimeError("knowledge store unavailable")), \
         patch("app.routes.workflow.retrieve_chatbot_context", side_effect=RuntimeError("pricing store unavailable")), \
         patch("app.routes.workflow.generate_structured", return_value={"success": True, "data": {"answer": "Cost-plus pricing starts with cost and a chosen margin."}}):
        result = chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"],
                                  question="How can I price this product?"), db, user)
    assert "Cost-plus pricing" in result["answer"]


def test_chat_prompt_uses_only_the_most_recent_sixteen_saved_messages(db):
    user = User(email="chat-bounded@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.flush()
    conversation = ChatConversation(retailer_id=user.id)
    db.add(conversation)
    db.flush()
    db.add_all([ChatMessage(conversation_id=conversation.id, role="user" if i % 2 == 0 else "assistant",
                            content=f"history item {i}") for i in range(20)])
    db.commit()
    with patch("app.routes.workflow.generate_structured", return_value={"success": True, "data": {"answer": "Okay."}}) as generate:
        chat(ChatRequest(conversation_id=conversation.id, question="Summarize?"), db, user)
    prompt = generate.call_args.args[0]
    assert "history item 3" not in prompt
    assert "history item 4" in prompt
    assert "history item 19" in prompt
    assert "CURRENT QUESTION:\nSummarize?" in prompt


def test_chat_routes_current_competitor_question_to_fresh_timestamped_evidence(db):
    from datetime import datetime, timedelta, timezone
    user, product = user_and_product(db, "fresh-market")
    listing = CompetitorProduct(catalog_product_id=product.catalog_product_id, platform_name="Amazon",
                                product_name="Comparable hair oil", price=175, availability=True,
                                scraped_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(listing)
    db.commit()
    observed = {}
    def answer(prompt, **_kwargs):
        observed["prompt"] = prompt
        return {"success": True, "data": {"answer": "One observed listing is ₹175; the sample is limited."}}
    with patch("app.services.competitor_service.get_competitor_freshness", return_value={"status": "FRESH"}), \
         patch("app.routes.workflow.generate_structured", side_effect=answer):
        result = chat(ChatRequest(retailer_product_id=product.id, question="What are competitors currently charging?"), db, user)
    assert "₹175.00" in observed["prompt"]
    assert "platform Amazon" in observed["prompt"]
    assert "scraped_at" in observed["prompt"]
    assert "Fresh observed listings: 1" in observed["prompt"]
    assert result["grounded"] is True


def test_chat_does_not_call_stale_competitor_row_current(db):
    from datetime import datetime, timedelta, timezone
    from app.services.market_analysis_service import build_market_evidence
    user, product = user_and_product(db, "stale-market")
    db.add(CompetitorProduct(catalog_product_id=product.catalog_product_id, platform_name="Flipkart",
                             product_name="Old listing", price=150, availability=True,
                             scraped_at=datetime.now(timezone.utc) - timedelta(days=30)))
    db.commit()
    evidence = build_market_evidence(db, product.catalog_product_id, "MARKET_ANALYSIS")
    assert "No marketplace listing is fresh enough" in evidence
    assert "Old listing" not in evidence


def test_historical_evidence_uses_persisted_observations_and_requires_repetition(db):
    from datetime import datetime, timedelta, timezone
    from app.services.market_analysis_service import build_market_evidence
    user, product = user_and_product(db, "history-market")
    listing = CompetitorProduct(catalog_product_id=product.catalog_product_id, platform_name="Meesho",
                                product_name="Comparable oil", price=160, availability=True,
                                scraped_at=datetime.now(timezone.utc) - timedelta(days=20))
    db.add(listing)
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all([CompetitorPriceHistory(competitor_product_id=listing.id, price=150, scraped_at=now-timedelta(days=10)),
                CompetitorPriceHistory(competitor_product_id=listing.id, price=160, scraped_at=now-timedelta(days=2))])
    db.commit()
    evidence = build_market_evidence(db, product.catalog_product_id, "HISTORICAL_TREND")
    assert "Historical observations in the last 90 days: 2 across 1 listings" in evidence
    assert "Direction is calculable for 1 listing" in evidence
    assert "not the whole market" in evidence


def test_no_selected_product_market_question_reports_no_evidence_and_keeps_context(db):
    from app.services.market_analysis_service import build_market_evidence
    evidence = build_market_evidence(db, None, "MARKET_ANALYSIS")
    assert "No product or resolved category is selected" in evidence
    assert "verified external source" in evidence
    user = User(email="chat-broad-market@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user)
    db.commit()
    with patch("app.routes.workflow.retrieve_business_knowledge", return_value="Organic products may support premium positioning when differentiated."), \
         patch("app.routes.workflow.generate_structured", return_value={"success": True, "data": {"answer": "I can explain implications, but need a category for current evidence."}}) as generate:
        chat(ChatRequest(question="Can you say anything about current trend and market analysis?"), db, user)
    prompt = generate.call_args.args[0]
    assert "No product or resolved category is selected" in prompt
    assert "Organic products may support premium positioning" in prompt
    assert "CURRENT QUESTION" in prompt


def test_market_followup_classification_uses_conversation_history():
    from app.services.market_analysis_service import classify_market_question
    assert classify_market_question("What about organic ones?", "User: What is the current trend for hair oils?") == "MARKET_ANALYSIS"


@pytest.mark.parametrize(("question", "knowledge_id"), [
    ("What is competitive pricing?", "competitive-positioning"),
    ("What is the difference between margin and markup?", "profit-margin-markup"),
    ("How should I reduce overstock?", "slow-moving-overstock-plan"),
    ("How should I plan a product launch?", "business-launch-checklist"),
    ("Should I use discounts?", "discount-promotions"),
    ("How does pack size affect pricing?", "same-pack-comparability"),
    ("How should I position an organic product?", "premium-value-evidence"),
    ("Should I increase my price?", "pricing-foundations"),
    ("How can I improve product profitability?", "contribution-per-order"),
    ("What should I consider before launching a new product?", "business-launch-checklist"),
])
def test_business_planning_questions_retrieve_matching_curated_knowledge(question, knowledge_id, monkeypatch):
    import json
    from app.services.ai import chroma_service

    documents = json.loads(chroma_service.BUSINESS_KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    document = next(item for item in documents if item["id"] == knowledge_id)
    observed = {}
    monkeypatch.setattr(chroma_service, "seed_business_knowledge", lambda: len(documents))
    def retrieve(_collection, query, n_results=5, where=None):
        observed["query"] = query
        return [{"text": document["text"], "metadata": {"topic": document["topic"]}}]
    monkeypatch.setattr(chroma_service, "retrieve_relevant", retrieve)
    result = chroma_service.retrieve_business_knowledge(question)
    assert result
    assert document["text"] in result
    if "overstock" in question:
        assert "dead" in observed["query"] and "stock" in observed["query"]


def test_business_knowledge_seeds_useful_category_metadata(monkeypatch):
    from app.services.ai import chroma_service

    captured = {}
    class Collection:
        def get(self, **_kwargs):
            return {"ids": []}
    monkeypatch.setattr(chroma_service, "_BUSINESS_KNOWLEDGE_DIGEST", None)
    monkeypatch.setattr(chroma_service, "get_collection", lambda _name: Collection())
    def store(_name, documents):
        captured["documents"] = documents
        return len(documents)
    monkeypatch.setattr(chroma_service, "store_documents_batch", store)
    count = chroma_service.seed_business_knowledge([{
        "id": "inventory-test", "topic": "Inventory Management",
        "text": "Stock planning and safety stock help reduce overstock.",
    }])
    assert count == 1
    metadata = captured["documents"][0]["metadata"]
    assert metadata["category"] == "inventory"
    assert "inventory" in metadata["categories"]
    assert metadata["source"] == "business_knowledge_seed"


def test_selected_product_business_planning_uses_actual_pricepulse_context(db):
    user, product = user_and_product(db, "business-context")
    conversation = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, user)
    observed = {}
    def generate(prompt, **_kwargs):
        observed["prompt"] = prompt
        return {"success": True, "data": {"answer": "Review cost, margin, stock and comparable pack-size prices before changing this product's offer."}}
    with patch("app.routes.workflow.retrieve_business_knowledge", return_value="Contribution margin compares revenue with variable costs."), \
         patch("app.routes.workflow.retrieve_chatbot_context", return_value=""), \
         patch("app.routes.workflow.generate_structured", side_effect=generate):
        result = chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"],
                                 question="How can I improve the profitability of this product?"), db, user)
    assert "Gundhal hair oil" in observed["prompt"]
    assert "cost: ₹100.00" in observed["prompt"]
    assert "stock quantity: 5 packs" in observed["prompt"]
    assert "Contribution margin compares revenue" in observed["prompt"]
    assert "historical unit-sales" in observed["prompt"]
    assert result["grounded"] is True


def test_portfolio_planning_is_owner_scoped_and_does_not_infer_best_seller(db):
    owner, product = user_and_product(db, "portfolio-owner")
    other, other_product = user_and_product(db, "portfolio-other")
    other_product.name_override = "Private other-user product"
    db.commit()
    catalog = ProductCatalog(name="Lavender soap", category="Bath", product_details="Organic soap")
    db.add(catalog); db.flush()
    second = RetailerProduct(user_id=owner.id, catalog_product_id=catalog.id, cost_price=75,
                             minimum_profit_margin=25, stock_quantity=18)
    db.add(second); db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, owner)
    observed = {}
    def generate(prompt, **_kwargs):
        observed["prompt"] = prompt
        return {"success": True, "data": {"answer": "Both products have stored stock, but sales history is unavailable."}}
    with patch("app.routes.workflow.retrieve_business_knowledge", return_value="Portfolio review should use dated sales and stock records."), \
         patch("app.routes.workflow.generate_structured", side_effect=generate):
        result = chat(ChatRequest(conversation_id=conversation["id"], question="Which of my products should I review first?"), db, owner)
    assert "Gundhal hair oil" in observed["prompt"]
    assert "Lavender soap" in observed["prompt"]
    assert other_product.product_name not in observed["prompt"]
    assert "No sales-volume history" in observed["prompt"]
    assert result["grounded"] is True


def test_chat_refuses_unsupported_numerical_forecast_without_sales_history(db):
    user = User(email="chat-forecast@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user); db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.generate_structured") as generate:
        result = chat(ChatRequest(conversation_id=conversation["id"], question="How many packs will I sell next month?"), db, user)
    assert "don't have enough historical sales-volume data" in result["answer"]
    assert "Stock on hand is not sales velocity" in result["answer"] or "sales-volume" in result["answer"]
    assert "dated unit sales" in result["answer"]
    generate.assert_not_called()


def test_best_seller_question_discloses_missing_sales_history(db):
    user = User(email="chat-best-seller@example.com", retailer_name="Retailer", password_hash="unused")
    db.add(user); db.commit()
    conversation = create_chat_conversation(ConversationRequest(), db, user)
    with patch("app.routes.workflow.generate_structured") as generate:
        result = chat(ChatRequest(conversation_id=conversation["id"], question="Which product is my best-selling product?"), db, user)
    assert "don't have sales-volume history" in result["answer"]
    assert "dated units sold" in result["answer"]
    generate.assert_not_called()


def test_competitor_context_exposes_only_compatible_unit_price_dimensions(db):
    user, product = user_and_product(db, "unit-prices")
    conversation = create_chat_conversation(ConversationRequest(retailer_product_id=product.id), db, user)
    db.add_all([
        CompetitorProduct(catalog_product_id=product.catalog_product_id, platform_name="Amazon",
                          product_name="Mass pack", price=120, quantity_value=100, quantity_unit="g", availability=True),
        CompetitorProduct(catalog_product_id=product.catalog_product_id, platform_name="Flipkart",
                          product_name="Volume pack", price=80, quantity_value=100, quantity_unit="ml", availability=True),
    ])
    db.commit()
    observed = {}
    def generate(prompt, **_kwargs):
        observed["prompt"] = prompt
        return {"success": True, "data": {"answer": "The observed unit prices are per gram and per millilitre and cannot be compared directly."}}
    with patch("app.routes.workflow.retrieve_chatbot_context", return_value=""), \
         patch("app.routes.workflow.generate_structured", side_effect=generate):
        chat(ChatRequest(retailer_product_id=product.id, conversation_id=conversation["id"],
                         question="Which competitor gives better value per unit?"), db, user)
    assert "unit price ₹1.2000 per g" in observed["prompt"]
    assert "unit price ₹0.8000 per ml" in observed["prompt"]
    assert "never rank unlike raw checkout prices" in observed["prompt"]
