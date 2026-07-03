"""Macro context: Yahoo top-stories → one Ollama summary per scan.

Runs ONCE per scan (not per signal). The resulting 2-3 sentence paragraph is
injected into every signal prompt so the advisor shares a consistent macro read.
Fail-open at every step: no headlines, or a summarizer error, yields ``""`` and
the signal prompt simply omits the macro section.
"""

from __future__ import annotations

import logging

import requests

from core.advisor.client import DEFAULT_ENDPOINT, DEFAULT_MODEL, ollama_chat
from core.advisor.news_fetcher import fetch_macro_headlines
from core.advisor.prompts import build_macro_summary_prompt

logger = logging.getLogger(__name__)

__all__ = ["build_market_context"]


def build_market_context(
        *,
        session: requests.Session | None = None,
        llm_session: requests.Session | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout: int = 20,
        max_headlines: int = 12,
) -> str:
    """Summarize current macro headlines into a short market-context paragraph."""
    headlines = fetch_macro_headlines(session=session, limit=max_headlines)
    if not headlines:
        return ""
    prompt = build_macro_summary_prompt(headlines, limit=max_headlines)
    summary = ollama_chat(
        prompt,
        endpoint=endpoint,
        model=model,
        timeout=timeout,
        temperature=0.2,
        max_tokens=200,
        session=llm_session,
    )
    if not summary:
        return ""
    return " ".join(summary.split()).strip()
