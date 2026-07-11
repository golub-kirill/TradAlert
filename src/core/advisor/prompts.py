"""Prompt construction for the advisor and its macro-context summarizer.

Both prompts are internal — never shown to the user. They carry NO dates or
timestamps (look-ahead hygiene) and ask for terse, decision-useful output.
"""

from __future__ import annotations

from core.advisor.schemas import AdvisorInput

__all__ = ["build_prompt", "build_macro_summary_prompt", "VERDICT_JSON_SCHEMA"]

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

_SYSTEM = (
    "You are a trading-signal advisor giving a second opinion on a technical "
    "entry that already fired. You never place orders and never override the "
    "human — you flag what a busy trader might miss. Judge this ticker on its "
    "own technical setup and its own news. Be skeptical of entries into earnings "
    "or fresh adverse news that is specifically about this ticker — not the "
    "general market mood. Keep reasoning to one or two sentences."
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


def build_prompt(ticker: str, input_data: AdvisorInput) -> str:
    """Build the full advisory prompt from signal context + news.

    Returns a single user-message string; the caller supplies the system role
    and the JSON `format` schema separately.
    """
    ticker_line = f"Ticker: {ticker}"
    if input_data.company_name:
        ticker_line += f"  (company: {input_data.company_name})"
    return (
        f"{_SYSTEM}\n\n"
        "## SIGNAL CONTEXT\n"
        f"{ticker_line}\n"
        f"Direction: {input_data.direction}\n"
        f"Signal Type: {input_data.signal_type}\n"
        f"Risk/Reward: {_fmt_num(input_data.min_rr)}:1\n"
        f"Market Regime: {input_data.market_regime or '—'}\n"
        f"Ticker Trend: {input_data.ticker_trend or '—'}\n"
        f"VIX Level: {_fmt_num(input_data.vix_level)}\n"
        f"Macro Score: {_fmt_num(input_data.macro_score)}\n"
        f"Behavioral Score: {_fmt_num(input_data.behavioral_score)}\n"
        f"Open Positions: {input_data.open_positions}\n"
        f"Data Tier: {input_data.tier or '—'}\n"
        f"Event Risk: {input_data.event_risk or '—'}\n"
        f"Setup Reason: {input_data.reason or '—'}\n\n"
        "## TECHNICAL POSTURE\n"
        f"{_fmt_posture(input_data)}\n\n"
        "## HISTORICAL EDGE (this setup's base rate over past resolved trades)\n"
        f"{_fmt_edge(input_data)}\n\n"
        "## MARKET CONTEXT (shared backdrop, already reflected in Market Regime "
        "above — do not disagree on this alone)\n"
        f"{input_data.market_context or '—'}\n\n"
        "## TICKER NEWS\n"
        f"{_fmt_headlines(input_data.headlines)}\n\n"
        "## INSTRUCTION\n"
        "Only news specifically about this ticker or its company moves your "
        "verdict. Headlines naming the company above, its subsidiaries, a named "
        "partner, or a top holding ARE about this ticker — never report an "
        "identity/asset mismatch just because a headline uses the company name "
        "instead of the symbol. Generic market or macro commentary (rates, the "
        "economy, other companies, broad 'bubble' talk) is backdrop, NOT "
        "ticker-adverse news — treat it as no news. Do not manufacture a risk to "
        "justify a 'disagree': if the setup is sound and there is no "
        "ticker-specific adverse news, 'agree' is the correct verdict.\n"
        "The HISTORICAL EDGE line is this setup's base rate; 'disagree' means "
        "this entry is materially worse than that base rate, not merely that some "
        "risk exists. Overextension (stretched RSI, far above the MA, a thin ATR "
        "buffer to the stop) and thin liquidity are flags to weigh, not automatic "
        "vetoes.\n"
        "Return a JSON object: verdict (agree|disagree|flag), confidence "
        "(0-1), reasoning (at most two short sentences on the technical + news "
        "picture), and risks (the single biggest risk to this entry in one "
        "short clause, or empty if none). "
        "'flag' means the entry is defensible but carries a risk the trader "
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
