"""
llm_service.py
==============
Centralized Ollama LLM Gateway for PricePulse.

Single point of contact for LLM calls (default model: qwen3:8b).
- JSON mode enforcement
- temperature 0
- thinking disabled for speed
- timeout controls
- one retry on bad JSON
- graceful error handling (never crashes)
"""

import json
import logging
import random
import re
import time
from typing import Any, Dict, Optional
import httpx
from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT

logger = logging.getLogger(__name__)

DEFAULT_MODEL = OLLAMA_MODEL
DEFAULT_TIMEOUT = OLLAMA_TIMEOUT


def _clean_json_text(text: str) -> str:
    """Strip markdown codeblocks and <think> tags if present."""
    if not text:
        return ""
    # Strip <think>...</think> if emitted
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    # Strip ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _sanitize_http_error_excerpt(body: str, sensitive_values=(), limit: int = 300) -> str:
    """Return a bounded diagnostic excerpt with request content and credentials redacted."""
    excerpt = body or ""
    for value in sensitive_values:
        if value:
            excerpt = excerpt.replace(str(value), "[REDACTED]")
    excerpt = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", excerpt)
    excerpt = re.sub(
        r"(?i)\b(authorization|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret)\b\s*[:=]\s*([^\s,;]+)",
        r"\1=[REDACTED]", excerpt,
    )
    excerpt = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", excerpt)
    excerpt = " ".join(excerpt.split())
    return excerpt[:limit]


def generate_structured(
    prompt: str,
    schema: Optional[Dict[str, Any]] = None,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    operation: str = "unspecified",
) -> Dict[str, Any]:
    """
    Execute a structured LLM query via Ollama and return parsed JSON data.

    Returns:
        {
            "success": bool,
            "data": dict | None,
            "error": str | None,
            "raw_response": str | None
        }
    Never raises an exception -- all failures return success=False with error details.
    """
    target_model = model or DEFAULT_MODEL
    req_timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)
    url = f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate"

    sys_instruction = system_prompt or (
        "You are an AI pricing and catalog intelligence engine. "
        "Always respond with strictly valid JSON matching the requested structure. "
        "Do not include explanation, markdown code fences, or thinking blocks."
    )

    full_prompt = prompt
    if schema:
        full_prompt += f"\n\nRequired JSON Schema:\n{json.dumps(schema, indent=2)}"

    payload: Dict[str, Any] = {
        "model": target_model,
        "prompt": full_prompt,
        "system": sys_instruction,
        "format": "json",
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.0,
        },
    }

    # One retry budget is shared by transport failures and JSON correction.
    # This prevents retries from multiplying when a retry itself returns bad JSON.
    max_attempts = 2
    raw_response_text = None
    started_at = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        logger.info("[LLM_REQUEST_STARTED] operation=%s attempt=%d/%d timeout=%ss",
                    operation, attempt, max_attempts, req_timeout)
        try:
            with httpx.Client(timeout=req_timeout) as client:
                resp = client.post(url, json=payload)

            if resp.status_code != 200:
                err_msg = f"Ollama HTTP {resp.status_code}"
                error_excerpt = _sanitize_http_error_excerpt(
                    resp.text,
                    sensitive_values=(full_prompt, sys_instruction),
                )
                if resp.status_code in {502, 503, 504} and attempt < max_attempts:
                    delay = 0.25 * (2 ** (attempt - 1)) + random.uniform(0, 0.15)
                    logger.warning("[LLM_RETRY] operation=%s attempt=%d category=http_%d elapsed_ms=%d backoff_ms=%d",
                                   operation, attempt, resp.status_code,
                                   int((time.monotonic() - started_at) * 1000), int(delay * 1000))
                    logger.warning("[LLM_HTTP_ERROR_DETAIL] operation=%s status=%d excerpt=%r",
                                   operation, resp.status_code, error_excerpt)
                    time.sleep(delay)
                    continue
                logger.warning("[LLM_REQUEST_FAILED] operation=%s attempt=%d category=http_%d elapsed_ms=%d",
                               operation, attempt, resp.status_code, int((time.monotonic() - started_at) * 1000))
                logger.warning("[LLM_HTTP_ERROR_DETAIL] operation=%s status=%d excerpt=%r",
                               operation, resp.status_code, error_excerpt)
                return {
                    "success": False,
                    "data": None,
                    "error": err_msg,
                    "raw_response": None,
                }

            res_json = resp.json()
            raw_response_text = res_json.get("response", "")
            cleaned = _clean_json_text(raw_response_text)

            parsed = json.loads(cleaned)
            logger.info("[LLM_REQUEST_SUCCESS] operation=%s attempt=%d elapsed_ms=%d",
                        operation, attempt, int((time.monotonic() - started_at) * 1000))
            return {
                "success": True,
                "data": parsed,
                "error": None,
                "raw_response": raw_response_text,
            }

        except (json.JSONDecodeError, ValueError) as json_err:
            logger.warning("[LLM_BAD_JSON] operation=%s attempt=%d/%d elapsed_ms=%d",
                           operation, attempt, max_attempts, int((time.monotonic() - started_at) * 1000))
            if attempt < max_attempts:
                # Augment prompt for single retry
                payload["prompt"] = (
                    f"{full_prompt}\n\n"
                    "CRITICAL: Your previous response was not valid JSON. "
                    "Return ONLY a single valid JSON object."
                )
                continue
            logger.error("[LLM_REQUEST_FAILED] operation=%s attempt=%d category=invalid_json elapsed_ms=%d",
                         operation, attempt, int((time.monotonic() - started_at) * 1000))
            return {
                "success": False,
                "data": None,
                "error": f"JSON decode error: {json_err}",
                "raw_response": raw_response_text,
            }

        except (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError):
            err_msg = "Ollama connection error"
            if attempt < max_attempts:
                delay = 0.25 * (2 ** (attempt - 1)) + random.uniform(0, 0.15)
                logger.warning("[LLM_RETRY] operation=%s attempt=%d category=connection elapsed_ms=%d backoff_ms=%d",
                               operation, attempt, int((time.monotonic() - started_at) * 1000), int(delay * 1000))
                time.sleep(delay)
                continue
            logger.error("[LLM_REQUEST_FAILED] operation=%s attempt=%d category=connection elapsed_ms=%d",
                         operation, attempt, int((time.monotonic() - started_at) * 1000))
            return {
                "success": False,
                "data": None,
                "error": err_msg,
                "raw_response": None,
            }

        except httpx.TimeoutException:
            err_msg = f"Ollama timeout ({req_timeout}s) exceeded"
            if attempt < max_attempts:
                delay = 0.25 * (2 ** (attempt - 1)) + random.uniform(0, 0.15)
                logger.warning("[LLM_RETRY] operation=%s attempt=%d category=timeout elapsed_ms=%d backoff_ms=%d",
                               operation, attempt, int((time.monotonic() - started_at) * 1000), int(delay * 1000))
                time.sleep(delay)
                continue
            logger.error("[LLM_TIMEOUT] operation=%s attempt=%d elapsed_ms=%d category=timeout %s",
                         operation, attempt, int((time.monotonic() - started_at) * 1000), err_msg)
            return {
                "success": False,
                "data": None,
                "error": err_msg,
                "raw_response": None,
            }

        except Exception as unexpected:
            err_msg = f"Unexpected LLM error ({type(unexpected).__name__})"
            logger.error("[LLM_UNEXPECTED_ERROR] operation=%s attempt=%d category=unexpected error_type=%s elapsed_ms=%d",
                         operation, attempt, type(unexpected).__name__, int((time.monotonic() - started_at) * 1000))
            return {
                "success": False,
                "data": None,
                "error": err_msg,
                "raw_response": None,
            }

    return {
        "success": False,
        "data": None,
        "error": "Exhausted LLM generation attempts",
        "raw_response": raw_response_text,
    }
