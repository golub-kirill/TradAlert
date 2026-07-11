"""Prompt construction for the advisor and its macro-context summarizer.

Both prompts are internal — never shown to the user. They carry NO dates or
timestamps (look-ahead hygiene) and ask for terse, decision-useful output.
"""

from __future__ import annotations

from core.advisor.schemas import AdvisorInput

__all__ = [
    "build_prompt", "build_macro_summary_prompt", "VERDICT_JSON_SCHEMA",
    "build_bull_prompt", "build_bear_prompt", "build_judge_prompt",
    "BULL_JSON_SCHEMA", "BEAR_JSON_SCHEMA",
]

# JSON schema handed to Ollama's structured-output `format` field. Grammar-
# constrained decoding guarantees the response parses — no fence-stripping.
VERDICT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["agree", "disagree", "flag"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
        "risks": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning", "risks"],
}

BULL_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["thesis", "points"],
}

BEAR_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "rebuttal": {"type": "string"},
    },
    "required": ["thesis", "points", "rebuttal"],
}

_SYSTEM = (
    "You are a trading-signal advisor giving a second opinion on a technical "
    "entry that already fired. You never place orders and never override the "
    "human — you flag what a busy trader might miss. Judge this ticker on its "
    "own technical setup and its own news. Be skeptical of entries into earnings "
    "or fresh adverse news that is specifically about this ticker — not the "
    "general market mood. Keep reasoning to one or two sentences."
)

_BULL_SYSTEM = (
    "You are the BULL analyst on a trading desk. Argue the strongest, "
    "evidence-based case FOR taking this already-fired entry: what makes the "
    "setup, its base rate, and any ticker-specific news attractive. Do not hedge "
    "into the bear case — that is the bear's job."
)

_BEAR_SYSTEM = (
    "You are the BEAR analyst — the critic — on a trading desk. Argue the "
    "strongest case AGAINST this entry and rebut the bull. Count only genuine "
    "setup weakness (overextension, a poor base rate, thin liquidity) and "
    "ticker-specific adverse news. Do not manufacture risk from the general "
    "market mood."
)

_JUDGE_SYSTEM = (
    "You are the JUDGE on a trading desk. You hold the bull case, the bear case, "
    "and this setup's historical base rate. The bull and bear were each ASSIGNED "
    "their side, so a forceful case from either is expected by default and is not "
    "itself evidence — discount both for advocacy and rule on the facts."
)

# Shared rule fragments reused across the single-shot and debate prompts.
_NEWS_RULE = (
    "Only news specifically about this ticker or its company counts. Headlines "
    "naming the company above, its subsidiaries, a named partner, or a top "
    "holding ARE about this ticker — never call it an identity/asset mismatch "
    "just because a headline uses the company name instead of the symbol. "
    "Generic market or macro commentary (rates, the economy, other companies, "
    "broad 'bubble' talk) is backdrop, NOT ticker-adverse news."
)
_EDGE_RULE = (
    "The HISTORICAL EDGE line is this setup's base rate; treat 'disagree' as "
    "'this entry is materially worse than that base rate,' not merely that some "
    "risk exists. Overextension (stretched RSI, far above the MA, a thin ATR "
    "buffer to the stop) and thin liquidity are flags to weigh, not vetoes."
)
# Judge-only: the bull/bear are assigned advocates, so a strong bear case is the
# baseline, not evidence. Default to the base rate; disagree needs a concrete edge.
_JUDGE_RULE = (
    "Default to 'agree' at the base rate and move to 'disagree' only when the bear "
    "names a concrete, ticker-specific adverse fact the bull cannot answer — not "
    "generic macro, a hypothetical, or a mere 'a risk exists'. When the bear has no "
    "ticker-specific edge over the bull, the assigned-adversary case is not grounds "
    "to disagree. Use 'flag' for a real but non-decisive ticker risk."
)


def _fmt_num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _fmt_headlines(headlines: list[dict], limit: int = 5) -> str:
    if not headlines:
        return "—"
    lines: list[str] = []
    for h in headlines[:limit]:
        head = str(h.get("headline") or h.get("title") or "").strip()
        if not head:
            continue
        src = str(h.get("source") or "").strip()
        lines.append(f"- {head}" + (f" ({src})" if src else ""))
    return "\n".join(lines) if lines else "—"


def _fmt_posture(d: AdvisorInput) -> str:
    """One-line technical posture — overextension + liquidity + location. Only the
    fields that are populated appear; ``—`` when the whole snapshot is missing."""
    bits: list[str] = []
    if d.rsi is not None:
        bits.append(f"RSI {d.rsi:.0f}")
    if d.pct_from_ma is not None:
        bits.append(f"{d.pct_from_ma:+.1f}% vs MA")
    if d.atr_to_stop is not None:
        bits.append(f"{d.atr_to_stop:.1f} ATR to stop")
    if d.atr_pct is not None:
        bits.append(f"ATR {d.atr_pct:.1f}%")
    liq = []
    if d.dv20 is not None:
        liq.append(f"${d.dv20 / 1e6:.1f}M/day")
    if d.cap_tier:
        liq.append(f"{d.cap_tier}-cap")
    if liq:
        bits.append("liquidity " + " ".join(liq))
    if d.rp_rank is not None:
        bits.append(f"location {d.rp_rank:.0f}/100")
    return "  |  ".join(bits) if bits else "—"


def _fmt_edge(d: AdvisorInput) -> str:
    from core.advisor.base_rates import format_base_rate

    return format_base_rate(d.base_rate) or "—"


def _fmt_case(case, *, with_rebuttal: bool = False) -> str:
    """Render a BullCase/BearCase (or None) into prompt text."""
    if case is None:
        return "—"
    lines = [case.thesis] if getattr(case, "thesis", "") else []
    lines += [f"- {p}" for p in getattr(case, "points", [])]
    if with_rebuttal and getattr(case, "rebuttal", ""):
        lines.append(f"Rebuttal: {case.rebuttal}")
    return "\n".join(lines) if lines else "—"


def _calibration_block(d: AdvisorInput) -> str:
    """The advisor's own recent calibration — only for the verdict-makers (the
    single-shot prompt and the judge). Empty string when there's no data yet."""
    return f"## RECENT CALIBRATION\n{d.reflection}\n\n" if d.reflection else ""


def _context_block(ticker: str, d: AdvisorInput) -> str:
    """The shared factual context (signal, posture, edge, market, news) embedded
    by both the single-shot prompt and every debate role. No instruction."""
    ticker_line = f"Ticker: {ticker}"
    if d.company_name:
        ticker_line += f"  (company: {d.company_name})"
    return (
        "## SIGNAL CONTEXT\n"
        f"{ticker_line}\n"
        f"Direction: {d.direction}\n"
        f"Signal Type: {d.signal_type}\n"
        f"Risk/Reward: {_fmt_num(d.min_rr)}:1\n"
        f"Market Regime: {d.market_regime or '—'}\n"
        f"Ticker Trend: {d.ticker_trend or '—'}\n"
        f"VIX Level: {_fmt_num(d.vix_level)}\n"
        f"Macro Score: {_fmt_num(d.macro_score)}\n"
        f"Behavioral Score: {_fmt_num(d.behavioral_score)}\n"
        f"Open Positions: {d.open_positions}\n"
        f"Data Tier: {d.tier or '—'}\n"
        f"Event Risk: {d.event_risk or '—'}\n"
        f"Setup Reason: {d.reason or '—'}\n\n"
        "## TECHNICAL POSTURE\n"
        f"{_fmt_posture(d)}\n\n"
        "## HISTORICAL EDGE (this setup's base rate over past resolved trades)\n"
        f"{_fmt_edge(d)}\n\n"
        "## MARKET CONTEXT (shared backdrop, already reflected in Market Regime "
        "above — do not disagree on this alone)\n"
        f"{d.market_context or '—'}\n\n"
        "## TICKER NEWS\n"
        f"{_fmt_headlines(d.headlines)}"
    )


def build_prompt(ticker: str, input_data: AdvisorInput) -> str:
    """Build the single-shot advisory prompt from signal context + news.

    Returns a single user-message string; the caller supplies the JSON `format`
    schema separately.
    """
    return (
        f"{_SYSTEM}\n\n"
        f"{_context_block(ticker, input_data)}\n\n"
        f"{_calibration_block(input_data)}"
        "## INSTRUCTION\n"
        f"{_NEWS_RULE} Do not manufacture a risk to justify a 'disagree': if the "
        "setup is sound and there is no ticker-specific adverse news, 'agree' is "
        "the correct verdict.\n"
        f"{_EDGE_RULE}\n"
        "Return a JSON object: verdict (agree|disagree|flag), confidence "
        "(0-1), reasoning (at most two short sentences on the technical + news "
        "picture), and risks (the single biggest risk to this entry in one "
        "short clause, or empty if none). "
        "'flag' means the entry is defensible but carries a risk the trader "
        "should see before committing."
    )


def build_bull_prompt(ticker: str, input_data: AdvisorInput) -> str:
    """Debate turn 1 — the bull's case FOR the entry."""
    return (
        f"{_BULL_SYSTEM}\n\n"
        f"{_context_block(ticker, input_data)}\n\n"
        "## INSTRUCTION\n"
        f"{_NEWS_RULE}\n"
        "Return JSON: thesis (one sentence — the core bull argument) and points "
        "(2-4 short strings of concrete supporting evidence)."
    )


def build_bear_prompt(ticker: str, input_data: AdvisorInput, bull) -> str:
    """Debate turn 2 — the bear/critic's case AGAINST, rebutting the bull."""
    return (
        f"{_BEAR_SYSTEM}\n\n"
        f"{_context_block(ticker, input_data)}\n\n"
        "## BULL CASE (rebut this)\n"
        f"{_fmt_case(bull)}\n\n"
        "## INSTRUCTION\n"
        f"{_NEWS_RULE} {_EDGE_RULE}\n"
        "Return JSON: thesis (one sentence — the core bear argument), points "
        "(2-4 short strings of concrete risks), and rebuttal (one sentence "
        "answering the bull's strongest point)."
    )


def build_judge_prompt(ticker: str, input_data: AdvisorInput, bull, bear,
                       *, risk_trichotomy: bool = True) -> str:
    """Debate turn 3 — the judge weighs both cases into a final verdict."""
    tri = (
        "Read the entry from a risk-seeking, a neutral, and a conservative desk's "
        "view, then commit to one verdict. "
        if risk_trichotomy else ""
    )
    return (
        f"{_JUDGE_SYSTEM}\n\n"
        f"{_context_block(ticker, input_data)}\n\n"
        "## BULL CASE\n"
        f"{_fmt_case(bull)}\n\n"
        "## BEAR CASE\n"
        f"{_fmt_case(bear, with_rebuttal=True)}\n\n"
        f"{_calibration_block(input_data)}"
        "## INSTRUCTION\n"
        f"{tri}{_JUDGE_RULE} {_EDGE_RULE} {_NEWS_RULE}\n"
        "Return a JSON object: verdict (agree|disagree|flag), confidence (0-1), "
        "reasoning (at most two short sentences weighing both cases against the "
        "base rate), and risks (the single biggest risk in one short clause, or "
        "empty if none). 'flag' means defensible but carrying a risk the trader "
        "should see before committing."
    )


def build_macro_summary_prompt(headlines: list[dict], limit: int = 12) -> str:
    """Prompt that condenses macro headlines into a 2-3 sentence context."""
    bullets = _fmt_headlines(headlines, limit=limit)
    return (
        "Summarize these financial headlines into a single 2-3 sentence market "
        "context paragraph. Focus on themes that affect US equities (rates, "
        "inflation, mega-cap tech, credit, energy). Skip individual stock "
        "mentions unless they are market-moving. Respond with ONLY the summary, "
        "no preamble.\n\n"
        f"Headlines:\n{bullets}"
    )
