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
    "human — you flag what a busy trader might miss. Weigh the technical setup "
    "against the market context and the ticker's recent news. Be skeptical of "
    "entries into earnings or fresh adverse news. Keep reasoning to one or two "
    "sentences."
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


def build_prompt(ticker: str, input_data: AdvisorInput) -> str:
    """Build the full advisory prompt from signal context + news.

    Returns a single user-message string; the caller supplies the system role
    and the JSON `format` schema separately.
    """
    return (
        f"{_SYSTEM}\n\n"
        "## SIGNAL CONTEXT\n"
        f"Ticker: {ticker}\n"
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
        "## MARKET CONTEXT\n"
        f"{input_data.market_context or '—'}\n\n"
        "## TICKER NEWS\n"
        f"{_fmt_headlines(input_data.headlines)}\n\n"
        "## INSTRUCTION\n"
        "Return a JSON object: verdict (agree|disagree|flag), confidence "
        "(0-1), reasoning (1-2 sentences on the technical + news picture), and "
        "risks (the single biggest risk to this entry, or empty if none). "
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
