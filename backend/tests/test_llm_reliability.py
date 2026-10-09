import asyncio
import asyncio
from unittest.mock import Mock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.product_catalog import ProductCatalog
from app.models.recommendation import Recommendation
from app.models.retailer_product import RetailerProduct
from app.models.user import User
from app.services import llm_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _response(text):
    response = Mock(status_code=200)
    response.json.return_value = {"response": text}
    return response


def _client_for(*outcomes):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post.side_effect = outcomes
    return client


def test_timeout_configuration_defaults_and_overrides(monkeypatch):
    from app import config

    monkeypatch.setenv("OLLAMA_TEST_TIMEOUT", "42.5")
    assert config._ollama_timeout("OLLAMA_TEST_TIMEOUT", 180) == 42.5
    monkeypatch.setenv("OLLAMA_TEST_TIMEOUT", "0")
    with pytest.raises(ValueError, match="positive number"):
        config._ollama_timeout("OLLAMA_TEST_TIMEOUT", 180)
    assert config._ollama_timeout("OLLAMA_MISSING_TIMEOUT", 180) == 180


def test_generate_structured_uses_explicit_operation_timeout():
    client = _client_for(_response('{"ok": true}'))
    with patch.object(llm_service.httpx, "Client", return_value=client) as make_client:
        result = llm_service.generate_structured("safe prompt", timeout=45, operation="scout_relevance")
    assert result["success"] is True
    assert make_client.call_args.kwargs["timeout"] == 45
    assert client.post.call_count == 1


def test_scout_relevance_uses_its_configured_operation_timeout():
    from app.services import relevance_filter_service as relevance

    relevance._RELEVANCE_CACHE.clear()
    with patch.object(relevance, "OLLAMA_RELEVANCE_TIMEOUT", 45), patch.object(
        relevance, "generate_structured", return_value={"success": True, "data": {"results": []}}
    ) as generate:
        relevance.filter_candidate_products("Herbal Hair Oil", "Hair care", [
            {"product_title": "Botanical Herbal Treatment Blend"}
        ])
    assert generate.call_args.kwargs["timeout"] == 45
    assert generate.call_args.kwargs["operation"] == "scout_relevance"
    relevance._RELEVANCE_CACHE.clear()


def test_transient_connection_failure_retries_once(monkeypatch):
    client = _client_for(httpx.ConnectError("offline"), _response('{"ok": true}'))
    monkeypatch.setattr(llm_service.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(llm_service.random, "uniform", lambda _a, _b: 0)
    with patch.object(llm_service.httpx, "Client", return_value=client):
        result = llm_service.generate_structured("safe prompt", operation="test")
    assert result["success"] is True
    assert client.post.call_count == 2


def test_persistent_timeout_stops_after_two_total_attempts(monkeypatch):
    client = _client_for(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    monkeypatch.setattr(llm_service.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(llm_service.random, "uniform", lambda _a, _b: 0)
    with patch.object(llm_service.httpx, "Client", return_value=client):
        result = llm_service.generate_structured("safe prompt", timeout=1, operation="test")
    assert result["success"] is False
    assert result["error"] == "Ollama timeout (1.0s) exceeded"
    assert client.post.call_count == 2


def test_invalid_json_uses_only_the_shared_single_retry_budget():
    client = _client_for(_response("not json"), _response("still not json"))
    monkeypatch_sleep = patch.object(llm_service.time, "sleep", lambda _delay: None)
    with monkeypatch_sleep, patch.object(llm_service.httpx, "Client", return_value=client):
        result = llm_service.generate_structured("safe prompt", operation="test")
    assert result["success"] is False
    assert client.post.call_count == 2


def test_cancellation_is_not_retried():
    client = _client_for(asyncio.CancelledError())
    with patch.object(llm_service.httpx, "Client", return_value=client):
        with pytest.raises(asyncio.CancelledError):
            llm_service.generate_structured("safe prompt", operation="test")
    assert client.post.call_count == 1


def test_temporary_service_unavailable_retries_once(monkeypatch):
    response = Mock(status_code=503, text="private response")
    client = _client_for(response, _response('{"ok": true}'))
    monkeypatch.setattr(llm_service.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(llm_service.random, "uniform", lambda _a, _b: 0)
    with patch.object(llm_service.httpx, "Client", return_value=client):
        result = llm_service.generate_structured("safe prompt", operation="test")
    assert result["success"] is True
    assert client.post.call_count == 2


def test_http_error_logs_bounded_sanitized_excerpt_and_keeps_public_error_generic(caplog):
    prompt = "PRIVATE_PRODUCT_PROMPT_7b5e"
    credential = "top-secret-token"
    response = Mock(
        status_code=500,
        text=(f"runtime failure; prompt={prompt}; Authorization: Bearer {credential}; "
              + "diagnostic detail " * 80),
    )
    client = _client_for(response)
    with patch.object(llm_service.httpx, "Client", return_value=client):
        result = llm_service.generate_structured(prompt, operation="test")

    assert result["success"] is False
    assert result["error"] == "Ollama HTTP 500"
    assert result["raw_response"] is None
    assert client.post.call_count == 1
    assert "status=500" in caplog.text
    assert "runtime failure" in caplog.text
    assert prompt not in caplog.text
    assert credential not in caplog.text
    detail_line = next(line for line in caplog.text.splitlines() if "[LLM_HTTP_ERROR_DETAIL]" in line)
    excerpt = detail_line.split("excerpt=", 1)[1]
    assert len(excerpt) <= 302  # 300 excerpt characters plus surrounding quotes


def test_permanent_http_error_is_not_retried():
    response = Mock(status_code=400, text="private response")
    client = _client_for(response)
    with patch.object(llm_service.httpx, "Client", return_value=client):
        result = llm_service.generate_structured("safe prompt", operation="test")
    assert result["success"] is False
    assert result["raw_response"] is None
    assert client.post.call_count == 1


def test_stream_endpoint_emits_error_event_and_does_not_persist_recommendation(db, monkeypatch):
    from app.routes import workflow

    user = User(email="stream@example.test", retailer_name="Test", password_hash="x")
    db.add(user)
    db.flush()
    catalog = ProductCatalog(name="Sample", category="Care")
    db.add(catalog)
    db.flush()
    product = RetailerProduct(user_id=user.id, catalog_product_id=catalog.id, cost_price=100,
                              stock_quantity=1, minimum_profit_margin=20)
    db.add(product)
    db.commit()
    monkeypatch.setattr(workflow, "analyze_retailer_product", lambda *args, **kwargs: {
        "error": "Price analysis could not be completed right now. Please try again."
    })
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)

    class ImmediateThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(workflow.threading, "Thread", ImmediateThread)

    async def inline_to_thread(function, *args):
        return function(*args)

    monkeypatch.setattr(workflow.asyncio, "to_thread", inline_to_thread)

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    response = workflow.analyze_stream(product.id, ConnectedRequest(), db, user)

    async def consume():
        parts = []
        async for part in response.body_iterator:
            parts.append(part)
        return "".join(parts)

    coroutine = consume()
    try:
        coroutine.send(None)
    except StopIteration as completed:
        wire = completed.value
    else:
        pytest.fail("mocked SSE iterator unexpectedly suspended")
    assert '"type": "error"' in wire
    assert '"type": "result"' not in wire
    assert db.query(Recommendation).count() == 0

    from fastapi import HTTPException
    workflow._active_workflows.add(product.id)
    try:
        with pytest.raises(HTTPException) as duplicate:
            workflow.analyze_stream(product.id, ConnectedRequest(), db, user)
        assert duplicate.value.status_code == 409
    finally:
        workflow._active_workflows.discard(product.id)
