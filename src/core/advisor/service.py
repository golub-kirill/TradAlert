"""Advisor orchestration — the single call site main.py uses.

``build_advisor_context`` runs once per scan (reads config, resolves API keys,
summarizes macro headlines). ``advise_signal`` runs per fired entry (loads/fetches
ticker news, calls the LLM, formats the note). Both are fail-open: a disabled
advisor, missing Ollama, or any error yields ``""`` and the signal is unaffected.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import requests

from core.advisor.client import DEFAULT_ENDPOINT, DEFAULT_MODEL, ask_llm
from core.advisor.macro_context import build_market_context
from core.advisor.news_cache import load_fresh_news, save_news
from core.advisor.news_fetcher import fetch_ticker_news, search_ticker_news
from core.advisor.schemas import AdvisorInput, AdvisorVerdict

logger = logging.getLogger(__name__)

__all__ = ["AdvisorContext", "build_advisor_context", "advise_signal", "format_note"]

_VERDICT_EMOJI = {"agree": "✅", "disagree": "❌", "flag": "⚠️"}
_MAX_NOTE = 500


@dataclass
class AdvisorContext:
    """Scan-wide advisor state, built once and reused across signals."""

    enabled: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout: int = 20
    temperature: float = 0.1
    max_tokens: int = 300
    cache_ttl_hours: float = 4.0
    max_headlines: int = 5
    market_context: str = ""
    finnhub_key: str | None = None
    brave_key: str | None = None
    # One keep-alive session for news HTTP across the whole scan.
    session: requests.Session = field(default_factory=requests.Session, repr=False)


def build_advisor_context(settings: dict | None) -> AdvisorContext:
    """Read config + env, summarize macro context. Fail-open → disabled context."""
    adv = (settings or {}).get("advisor") or {}
    if not adv.get("enabled", False):
        return AdvisorContext(enabled=False)

    news_cfg = (settings or {}).get("news") or {}
    ctx = AdvisorContext(
        enabled=True,
        endpoint=str(adv.get("endpoint", DEFAULT_ENDPOINT)),
        model=str(adv.get("model", DEFAULT_MODEL)),
        timeout=int(adv.get("timeout", 20)),
        temperature=float(adv.get("temperature", 0.1)),
        max_tokens=int(adv.get("max_tokens", 300)),
        cache_ttl_hours=float(news_cfg.get("cache_ttl_hours", 4.0)),
        max_headlines=int(news_cfg.get("max_headlines_per_ticker", 5)),
        finnhub_key=os.environ.get("FINNHUB_API_KEY") or None,
        brave_key=os.environ.get("BRAVE_API_KEY") or None,
    )
    if news_cfg.get("macro_summarization", True):
        try:
            ctx.market_context = build_market_context(
                session=ctx.session,
                llm_session=ctx.session,
                endpoint=ctx.endpoint,
                model=ctx.model,
                timeout=ctx.timeout,
            )
        except Exception as exc:  # fail-open — never block the scan
            logger.warning("advisor macro context failed — skipped: %s", exc)
    return ctx


def _resolve_headlines(ticker: str, ctx: AdvisorContext) -> list[dict]:
    """Cache → Finnhub/Yahoo (cached) → Brave (cached). ``[]`` if all dry."""
    heads = load_fresh_news(ticker, staleness_hours=ctx.cache_ttl_hours)
    if heads:
        return heads
    heads = fetch_ticker_news(
        ticker, finnhub_key=ctx.finnhub_key, session=ctx.session, limit=ctx.max_headlines
    )
    if heads:
        save_news(ticker, "finnhub", heads)
        return heads
    heads = search_ticker_news(ticker, brave_key=ctx.brave_key, session=ctx.session,
                               limit=ctx.max_headlines)
    if heads:
        save_news(ticker, "search", heads)
    return heads


def format_note(verdict: AdvisorVerdict) -> str:
    """Render a verdict into the display string stored on the signal."""
    emoji = _VERDICT_EMOJI.get(verdict.verdict, "🤖")
    note = f"{emoji} {verdict.verdict.capitalize()} · {verdict.confidence:.0%} — {verdict.reasoning}"
    if verdict.risks:
        note += f"  ⚠ {verdict.risks}"
    return note[:_MAX_NOTE]


def advise_signal(
        ticker: str,
        signal,
        ctx: AdvisorContext,
        *,
        vix_level: float | None = None,
        macro_score: float | None = None,
        behavioral_score: float | None = None,
        open_positions: int = 0,
) -> str:
    """Review one fired entry. Returns the advisor note, or ``""`` on any failure."""
    if not ctx.enabled:
        return ""
    try:
        headlines = _resolve_headlines(ticker, ctx)
        input_data = AdvisorInput(
            ticker=ticker,
            direction=signal.direction,
            signal_type=signal.signal_type,
            stop_price=float(signal.stop_price),
            target_price=float(signal.target_price),
            min_rr=float(signal.min_rr),
            market_regime=signal.market_regime,
            ticker_trend=signal.ticker_trend,
            reason=signal.reason,
            tier=getattr(signal, "tier", "LIVE"),
            event_risk=getattr(signal, "event_risk", ""),
            vix_level=vix_level,
            macro_score=macro_score,
            behavioral_score=behavioral_score,
            open_positions=open_positions,
            market_context=ctx.market_context,
            headlines=headlines,
        )
        verdict = ask_llm(
            input_data,
            endpoint=ctx.endpoint,
            model=ctx.model,
            timeout=ctx.timeout,
            temperature=ctx.temperature,
            max_tokens=ctx.max_tokens,
            session=ctx.session,
        )
        return format_note(verdict) if verdict else ""
    except Exception as exc:  # fail-open — advisor never breaks a scan
        logger.warning("advisor skipped for %s — %s", ticker, exc)
        return ""
