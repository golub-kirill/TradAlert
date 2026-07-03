"""Ollama HTTP client for the advisor — fail-open by contract.

Every entry point returns ``None`` (never raises) on any failure: Ollama down,
timeout, non-JSON, malformed fields. The caller treats ``None`` as "no advice"
and the signal fires exactly as it would with the advisor disabled.

Two Ollama-specific details that make this reliable:
- ``format`` is a JSON schema, so decoding is grammar-constrained — the response
  is guaranteed to parse (no markdown-fence stripping needed).
- ``think: false`` disables Qwen3-style reasoning tokens, which would otherwise
  consume the whole ``num_predict`` budget and return empty content.
"""

from __future__ import annotations

import json
import logging

import requests

from core.advisor.prompts import VERDICT_JSON_SCHEMA, build_prompt
from core.advisor.schemas import AdvisorInput, AdvisorVerdict

logger = logging.getLogger(__name__)

__all__ = ["ask_llm", "ollama_chat", "DEFAULT_ENDPOINT", "DEFAULT_MODEL"]

DEFAULT_ENDPOINT = "http://localhost:11434"
# qwen3.5:9b won the 3-scenario judgment probe (2026-07-02) over qwen3:8b/14b:
# decisive codex-aligned verdicts, ~1.7s warm, 100% GPU on 12 GB.
DEFAULT_MODEL = "qwen3.5:9b"

# One keep-alive session for the whole process. All advisor calls hit the same
# localhost Ollama, so connection reuse trims per-call overhead across a scan.
_session: requests.Session | None = None


def _get_session(session: requests.Session | None) -> requests.Session:
    global _session
    if session is not None:
        return session
    if _session is None:
        _session = requests.Session()
    return _session


def ollama_chat(
        prompt: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: int = 20,
        temperature: float = 0.1,
        max_tokens: int = 300,
        fmt: dict | None = None,
        session: requests.Session | None = None,
) -> str | None:
    """Single-turn Ollama chat. Returns the assistant message content, or None.

    ``fmt`` is an optional JSON schema for structured output. Fail-open: any
    transport, HTTP, or decode error logs a WARNING and returns None.
    """
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if fmt is not None:
        body["format"] = fmt

    try:
        resp = _get_session(session).post(
            f"{endpoint.rstrip('/')}/api/chat", json=body, timeout=timeout
        )
        resp.raise_for_status()
        content = (resp.json().get("message") or {}).get("content")
    except requests.exceptions.ConnectionError:
        logger.warning("advisor unreachable at %s — skipped", endpoint)
        return None
    except requests.exceptions.Timeout:
        logger.warning("advisor timeout (>%ds) — skipped", timeout)
        return None
    except requests.exceptions.HTTPError as exc:
        logger.warning("advisor HTTP error (%s) — skipped", exc)
        return None
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("advisor response decode failed — skipped: %s", exc)
        return None

    if not content or not str(content).strip():
        logger.warning("advisor returned empty content — skipped")
        return None
    return str(content)


def _parse(raw: str) -> AdvisorVerdict | None:
    """Parse a schema-constrained JSON response into an AdvisorVerdict."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("advisor JSON parse failed — skipped: %s", str(raw)[:160])
        return None
    try:
        return AdvisorVerdict(
            verdict=str(data.get("verdict", "")).lower().strip(),
            confidence=data.get("confidence", 0.0),
            reasoning=data.get("reasoning", ""),
            risks=data.get("risks", ""),
        )
    except (ValueError, TypeError) as exc:
        logger.warning("advisor verdict invalid — skipped: %s", exc)
        return None


def ask_llm(
        input_data: AdvisorInput,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: int = 20,
        temperature: float = 0.1,
        max_tokens: int = 300,
        session: requests.Session | None = None,
) -> AdvisorVerdict | None:
    """Ask the LLM to review one fired signal. Returns a verdict or None."""
    prompt = build_prompt(input_data.ticker, input_data)
    raw = ollama_chat(
        prompt,
        endpoint=endpoint,
        model=model,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
        fmt=VERDICT_JSON_SCHEMA,
        session=session,
    )
    if raw is None:
        return None
    return _parse(raw)
