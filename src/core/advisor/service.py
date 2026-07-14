"""Advisor orchestration — the single call site main.py uses.

``build_advisor_context`` runs once per scan (reads config, resolves API keys,
summarizes macro headlines). ``advise_signal`` runs per fired entry: it scores
the deterministic rubric, gathers + classifies ticker news, and formats the note.
Fail-open — a disabled advisor or any error yields ``""`` and the signal is
unaffected; when only Ollama is unreachable the rubric still returns a note
(news marked unread, conviction capped).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import requests

from core.advisor.base_rates import load_base_rates
from core.advisor.base_rates import lookup as _lookup_base_rate
from core.advisor.ollama_client import DEFAULT_ENDPOINT, DEFAULT_MODEL, classify_news
from core.advisor.macro_context import build_market_context
from core.advisor.news_cache import load_fresh_news, save_news
from core.advisor.news_fetcher import gather_ticker_news
from core.advisor.news_query import build_queries, generate_queries, split_headlines
from core.advisor.rubric import apply_news, score_rubric
from core.advisor.schemas import AdvisorInput, AdvisorVerdict, NewsRead

logger = logging.getLogger(__name__)

__all__ = ["AdvisorContext", "build_advisor_context", "build_advisor_input",
           "build_verdict", "advise_signal", "format_note"]

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
    cache_ttl_hours: float = 4.0
    max_headlines: int = 5
    market_context: str = ""
    finnhub_key: str | None = None
    brave_key: str | None = None
    alphavantage_key: str | None = None
    # AlphaVantage free tier is 25/day — budget calls across scans; 0 disables it.
    use_alphavantage: bool = True
    av_max_per_day: int = 20
    # LLM-generated (cached) news queries; off by default (adds a call per new ticker).
    use_llm_queries: bool = False
    # ticker -> full company name (warmed by scripts/fetch/fetch_company_names.py).
    company_names: dict = field(default_factory=dict)
    # setup base-rate table (scripts/studies/build_advisor_base_rates.py); {} = off.
    base_rates: dict = field(default_factory=dict)
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
        alphavantage_key=os.environ.get("ALPHAVANTAGE_API_KEY") or None,
        use_alphavantage=bool(news_cfg.get("use_alphavantage", True)),
        av_max_per_day=int(news_cfg.get("alphavantage_max_per_day", 20)),
        use_llm_queries=bool(news_cfg.get("llm_queries", False)),
        company_names=_load_company_names(),
        base_rates=load_base_rates(),
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


def _resolve_queries(ticker: str, company_name: str, ctx: AdvisorContext) -> list[dict]:
    """News search queries — deterministic by default; optional cached LLM-built
    queries when ``advisor`` config enables ``news.llm_queries``."""
    if not getattr(ctx, "use_llm_queries", False):
        return build_queries(ticker, company_name)
    import json

    from core.paths import NEWS_DIR
    path = NEWS_DIR / ".queries.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if isinstance(cache, dict) and cache.get(ticker):
        return cache[ticker]
    qs = generate_queries(ticker, company_name, endpoint=ctx.endpoint,
                          model=ctx.model, timeout=ctx.timeout, session=ctx.session)
    if not qs:
        return build_queries(ticker, company_name)
    if not ctx.read_only:
        try:
            cache = cache if isinstance(cache, dict) else {}
            cache[ticker] = qs
            NEWS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except OSError:
            pass
    return qs


def _resolve_headlines(ticker: str, ctx: AdvisorContext, company_name: str = "") -> list[dict]:
    """Fresh cache → merged multi-source gather (Google News on the company name,
    Finnhub, AlphaVantage, backstops), relevance-filtered. Read-only skips the
    cache read/write entirely (a diagnostic path must not mutate data/news/)."""
    # news_cache._read quarantines corrupt files, which is a write. A diagnostic
    # read-only context must not even inspect it; fetch fresh headlines instead.
    heads = [] if ctx.read_only else load_fresh_news(ticker, staleness_hours=ctx.cache_ttl_hours)
    if heads:
        return heads
    heads = gather_ticker_news(
        ticker, company_name,
        finnhub_key=ctx.finnhub_key, alphavantage_key=ctx.alphavantage_key,
        brave_key=ctx.brave_key, session=ctx.session, limit=ctx.max_headlines,
        queries=_resolve_queries(ticker, company_name, ctx),
        use_alphavantage=ctx.use_alphavantage, av_max_per_day=ctx.av_max_per_day,
    )
    if heads and not ctx.read_only:
        save_news(ticker, "gathered", heads)
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


# Compact per-axis scorecard glyphs — the descriptive "why" at a glance.
_AXIS_LABEL = {"edge": "edge", "alignment": "trend", "overextension": "ext",
               "liquidity": "liq", "rr": "R:R", "event": "event"}


def _scorecard(rubric: dict) -> str:
    """One-line axis checklist: ``edge✓ trend✓ ext· liq✗ R:R✓ event·``.

    ✓ = supports the call, ✗ = counts against it, · = neutral/unknown. Rendered
    from the rubric breakdown so a trader sees which axes drove the verdict."""
    crits = (rubric or {}).get("criteria") or {}
    parts = []
    for name in ("edge", "alignment", "overextension", "liquidity", "rr", "event"):
        cell = crits.get(name)
        if not cell:
            continue
        pts = cell.get("points", 0)
        glyph = "✓" if pts > 0 else ("✗" if pts < 0 else "·")
        parts.append(f"{_AXIS_LABEL[name]}{glyph}")
    return " ".join(parts)


def format_note(verdict: AdvisorVerdict) -> str:
    """Render a verdict into the display string stored on the signal.

    Conviction is shown as an N/10 score, not a percentage — it is a rubric-
    derived conviction, not a win probability, and a % invited that misread. The
    per-axis scorecard gives the "why"; reasoning and risks are clipped at a word
    boundary so the card never shows a mid-word cut (``_MAX_NOTE`` is the net).
    """
    emoji = _VERDICT_EMOJI.get(verdict.verdict, "🤖")
    conviction = max(1, int(verdict.confidence * 10 + 0.5))
    reasoning = _clip(verdict.reasoning, _MAX_REASONING)
    note = f"{emoji} {verdict.verdict.capitalize()} · {conviction}/10 — {reasoning}"
    card = _scorecard(verdict.rubric)
    if card:
        note += f"  [{card}]"
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
    company_name = (ctx.company_names.get(ticker)
                    or ctx.company_names.get(ticker.upper()) or "")
    if headlines is None:
        headlines = _resolve_headlines(ticker, ctx, company_name)

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
    )


def build_verdict(input_data: AdvisorInput, ctx: AdvisorContext) -> AdvisorVerdict:
    """Hybrid verdict: the deterministic rubric computes the technical call and
    calibrated confidence; the LLM contributes only the news read, which can add
    caution (downgrade / veto on adverse catalysts, penalize when blind) but
    never inflate a weak setup. Always returns a verdict — the rubric stands on
    its own when the model is unreachable."""
    rubric = score_rubric(input_data)
    catalysts, _recaps = split_headlines(input_data.headlines or [])

    news = None
    if catalysts:
        news = classify_news(
            input_data.ticker, input_data.company_name, input_data.direction,
            catalysts, endpoint=ctx.endpoint, model=ctx.model, timeout=ctx.timeout,
            temperature=ctx.temperature, max_tokens=min(ctx.max_tokens, 200),
            session=ctx.session,
        )
    if news is None:
        # No catalyst headlines → genuinely no orthogonal news ('none'); catalysts
        # present but the model unreachable → 'unknown' (blind, so stay humble).
        news = NewsRead(stance="unknown" if catalysts else "none")

    rubric = apply_news(rubric, news.stance, news.severity, news.material_news)
    return AdvisorVerdict(
        verdict=rubric.verdict, confidence=rubric.confidence,
        reasoning=rubric.reasoning, risks=rubric.risk,
        rubric=rubric.to_dict(), news_stance=news.stance,
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
        verdict = build_verdict(input_data, ctx)
        return format_note(verdict) if verdict else ""
    except Exception as exc:  # fail-open — advisor never breaks a scan
        logger.warning("advisor skipped for %s — %s", ticker, exc)
        return ""
