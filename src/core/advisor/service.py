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

from core.advisor.base_rates import load_base_rates
from core.advisor.base_rates import lookup as _lookup_base_rate
from core.advisor.client import DEFAULT_ENDPOINT, DEFAULT_MODEL, ask_llm
from core.advisor.macro_context import build_market_context
from core.advisor.news_cache import load_fresh_news, save_news
from core.advisor.news_fetcher import fetch_ticker_news, search_ticker_news
from core.advisor.reflection import format_reflection, load_reflection
from core.advisor.schemas import AdvisorInput, AdvisorVerdict

logger = logging.getLogger(__name__)

__all__ = ["AdvisorContext", "build_advisor_context", "build_advisor_input",
           "advise_signal", "format_note"]

_VERDICT_EMOJI = {"agree": "✅", "disagree": "❌", "flag": "⚠️"}
_MAX_NOTE = 500  # hard ceiling on the rendered note (safety net)
_MAX_REASONING = 320  # per-field budgets, clipped at a word boundary
_MAX_RISK = 150


@dataclass
class AdvisorContext:
    """Scan-wide advisor state, built once and reused across signals."""

    enabled: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout: int = 20
    temperature: float = 0.1
    max_tokens: int = 300
    # Multi-agent bull/bear/judge critic (off by default; single-shot otherwise).
    debate_enabled: bool = False
    debate_risk_trichotomy: bool = True
    debate_total_timeout: float = 0.0  # 0 = no wall-clock cap
    cache_ttl_hours: float = 4.0
    max_headlines: int = 5
    market_context: str = ""
    finnhub_key: str | None = None
    brave_key: str | None = None
    # ticker -> full company name (warmed by scripts/fetch/fetch_company_names.py).
    company_names: dict = field(default_factory=dict)
    # setup base-rate table (scripts/studies/build_advisor_base_rates.py); {} = off.
    base_rates: dict = field(default_factory=dict)
    # advisor's own recent calibration line (build_advisor_calibration.py); "" = off.
    reflection: str = ""
    # Diagnostic callers can fetch current news without mutating data/news/.
    read_only: bool = False
    # One keep-alive session for news HTTP across the whole scan.
    session: requests.Session = field(default_factory=requests.Session, repr=False)


def _load_company_names() -> dict:
    """ticker -> full company name (data/company_names.json). Fail-open → {}."""
    import json

    from core.paths import DATA_DIR
    try:
        with open(DATA_DIR / "company_names.json", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # missing/corrupt file is non-fatal — advisor still runs
        return {}


def build_advisor_context(
        settings: dict | None,
        *,
        read_only: bool = False,
) -> AdvisorContext:
    """Read config + env, summarize macro context. Fail-open → disabled context."""
    adv = (settings or {}).get("advisor") or {}
    if not adv.get("enabled", False):
        return AdvisorContext(enabled=False, read_only=read_only)

    news_cfg = (settings or {}).get("news") or {}
    deb = adv.get("debate") or {}
    ctx = AdvisorContext(
        enabled=True,
        endpoint=str(adv.get("endpoint", DEFAULT_ENDPOINT)),
        model=str(adv.get("model", DEFAULT_MODEL)),
        timeout=int(adv.get("timeout", 20)),
        temperature=float(adv.get("temperature", 0.1)),
        max_tokens=int(adv.get("max_tokens", 300)),
        debate_enabled=bool(deb.get("enabled", False)),
        debate_risk_trichotomy=bool(deb.get("risk_trichotomy", True)),
        debate_total_timeout=float(deb.get("total_timeout", 0) or 0),
        cache_ttl_hours=float(news_cfg.get("cache_ttl_hours", 4.0)),
        max_headlines=int(news_cfg.get("max_headlines_per_ticker", 5)),
        finnhub_key=os.environ.get("FINNHUB_API_KEY") or None,
        brave_key=os.environ.get("BRAVE_API_KEY") or None,
        company_names=_load_company_names(),
        base_rates=load_base_rates(),
        reflection=format_reflection(load_reflection()),
        read_only=read_only,
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
    """Cache → Finnhub/Yahoo → Brave, or an entirely cache-free read-only path."""
    # news_cache._read quarantines corrupt files, which is a write. A diagnostic
    # read-only context must not even inspect it; fetch fresh headlines instead.
    heads = [] if ctx.read_only else load_fresh_news(ticker, staleness_hours=ctx.cache_ttl_hours)
    if heads:
        return heads
    heads = fetch_ticker_news(
        ticker, finnhub_key=ctx.finnhub_key, session=ctx.session, limit=ctx.max_headlines
    )
    if heads:
        if not ctx.read_only:
            save_news(ticker, "finnhub", heads)
        return heads
    heads = search_ticker_news(ticker, brave_key=ctx.brave_key, session=ctx.session,
                               limit=ctx.max_headlines)
    if heads:
        if not ctx.read_only:
            save_news(ticker, "search", heads)
    return heads


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` chars at a word boundary, appending an ellipsis.

    A plain slice cut display text mid-word ("…non-US assets d"); this trims back
    to the last space and drops trailing punctuation before the ellipsis.
    """
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return (head or text[:limit].rstrip()) + "…"


def format_note(verdict: AdvisorVerdict) -> str:
    """Render a verdict into the display string stored on the signal.

    Reasoning and risks are each clipped to a per-field budget at a word boundary
    so the card never shows a mid-word cut; ``_MAX_NOTE`` is a final safety net.
    """
    emoji = _VERDICT_EMOJI.get(verdict.verdict, "🤖")
    reasoning = _clip(verdict.reasoning, _MAX_REASONING)
    note = f"{emoji} {verdict.verdict.capitalize()} · {verdict.confidence:.0%} — {reasoning}"
    if verdict.risks:
        note += f"  ⚠ {_clip(verdict.risks, _MAX_RISK)}"
    return note[:_MAX_NOTE]


def _sfloat(value) -> float | None:
    """float() that returns None instead of raising on None/garbage."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _cap_tier(market_cap: float | None) -> str:
    """Coarse US-equity market-cap bucket; ``""`` when unknown."""
    if market_cap is None:
        return ""
    if market_cap >= 10e9:
        return "large"
    if market_cap >= 2e9:
        return "mid"
    if market_cap >= 300e6:
        return "small"
    return "micro"


def build_advisor_input(
        ticker: str,
        signal,
        ctx: AdvisorContext,
        *,
        scan=None,
        pct_from_ma: float | None = None,
        rp_rank: float | None = None,
        vix_level: float | None = None,
        macro_score: float | None = None,
        behavioral_score: float | None = None,
        open_positions: int = 0,
        headlines: list[dict] | None = None,
) -> AdvisorInput:
    """Assemble the AdvisorInput — the single construction path shared by the live
    call (``advise_signal``) and the offline smoke test, so new fields land once.

    Posture fields read from the ScanResult snapshot and degrade to None when
    ``scan`` is absent (e.g. the historical-trade smoke test). ``atr_to_stop`` is
    derived from the snapshot; ``pct_from_ma``/``rp_rank`` are supplied by the
    caller (they need the DataFrame / rank table not carried on the scan).
    """
    if headlines is None:
        headlines = _resolve_headlines(ticker, ctx)
    company_name = (ctx.company_names.get(ticker)
                    or ctx.company_names.get(ticker.upper()) or "")

    close = _sfloat(getattr(scan, "close", None))
    atr = _sfloat(getattr(scan, "atr", None))
    stop = _sfloat(getattr(signal, "stop_price", None))
    atr_to_stop = (abs(close - stop) / atr
                   if close is not None and atr and stop is not None else None)
    market_cap = _sfloat(getattr(scan, "market_cap", None))
    base_rate = _lookup_base_rate(
        ctx.base_rates, signal.signal_type, signal.market_regime, signal.ticker_trend)

    return AdvisorInput(
        ticker=ticker,
        company_name=company_name,
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
        rsi=_sfloat(getattr(scan, "rsi", None)),
        atr_pct=_sfloat(getattr(scan, "atr_pct", None)),
        pct_from_ma=_sfloat(pct_from_ma),
        atr_to_stop=atr_to_stop,
        dv20=_sfloat(getattr(scan, "dv20", None)),
        market_cap=market_cap,
        cap_tier=_cap_tier(market_cap),
        rp_rank=_sfloat(rp_rank),
        base_rate=base_rate,
        reflection=getattr(ctx, "reflection", "") or "",
    )


def advise_signal(
        ticker: str,
        signal,
        ctx: AdvisorContext,
        *,
        scan=None,
        pct_from_ma: float | None = None,
        rp_rank: float | None = None,
        vix_level: float | None = None,
        macro_score: float | None = None,
        behavioral_score: float | None = None,
        open_positions: int = 0,
) -> str:
    """Review one fired entry. Returns the advisor note, or ``""`` on any failure."""
    if not ctx.enabled:
        return ""
    try:
        input_data = build_advisor_input(
            ticker, signal, ctx,
            scan=scan, pct_from_ma=pct_from_ma, rp_rank=rp_rank,
            vix_level=vix_level, macro_score=macro_score,
            behavioral_score=behavioral_score, open_positions=open_positions,
        )
        if ctx.debate_enabled:
            from core.advisor.debate import run_debate
            verdict = run_debate(input_data, ctx).verdict
        else:
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
