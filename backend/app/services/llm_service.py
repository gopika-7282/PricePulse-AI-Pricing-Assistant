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
import os
import re
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
DEFAULT_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "30.0"))


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


def generate_structured(
    prompt: str,
    schema: Optional[Dict[str, Any]] = None,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
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
    req_timeout = timeout or DEFAULT_TIMEOUT
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
        "options": {
            "temperature": 0.0,
        },
    }

    max_attempts = 2
    raw_response_text = None

    for attempt in range(1, max_attempts + 1):
        logger.info(
            f"[LLM_REQUEST_STARTED] model='{target_model}' attempt={attempt}/{max_attempts} "
            f"timeout={req_timeout}s"
        )
        try:
            with httpx.Client(timeout=req_timeout) as client:
                resp = client.post(url, json=payload)

            if resp.status_code != 200:
                err_msg = f"Ollama HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(f"[LLM_REQUEST_FAILED] {err_msg}")
                return {
                    "success": False,
                    "data": None,
                    "error": err_msg,
                    "raw_response": resp.text if resp else None,
                }

            res_json = resp.json()
            raw_response_text = res_json.get("response", "")
            cleaned = _clean_json_text(raw_response_text)

            parsed = json.loads(cleaned)
            logger.info(f"[LLM_REQUEST_SUCCESS] model='{target_model}' attempt={attempt}")
            return {
                "success": True,
                "data": parsed,
                "error": None,
                "raw_response": raw_response_text,
            }

        except (json.JSONDecodeError, ValueError) as json_err:
            logger.warning(
                f"[LLM_BAD_JSON] attempt={attempt}/{max_attempts} error='{json_err}' "
                f"raw_sample='{raw_response_text[:150] if raw_response_text else ''}'"
            )
            if attempt < max_attempts:
                # Augment prompt for single retry
                payload["prompt"] = (
                    f"{full_prompt}\n\n"
                    "CRITICAL: Your previous response was not valid JSON. "
                    "Return ONLY a single valid JSON object."
                )
                continue
            return {
                "success": False,
                "data": None,
                "error": f"JSON decode error: {json_err}",
                "raw_response": raw_response_text,
            }

        except (httpx.ConnectError, httpx.NetworkError) as conn_err:
            err_msg = f"Ollama connection error: {conn_err}"
            logger.error(f"[LLM_CONNECTION_ERROR] {err_msg}")
            return {
                "success": False,
                "data": None,
                "error": err_msg,
                "raw_response": None,
            }

        except httpx.TimeoutException as timeout_err:
            err_msg = f"Ollama timeout ({req_timeout}s) exceeded: {timeout_err}"
            logger.error(f"[LLM_TIMEOUT] {err_msg}")
            return {
                "success": False,
                "data": None,
                "error": err_msg,
                "raw_response": None,
            }

        except Exception as unexpected:
            err_msg = f"Unexpected LLM error: {unexpected}"
            logger.error(f"[LLM_UNEXPECTED_ERROR] {err_msg}", exc_info=True)
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
