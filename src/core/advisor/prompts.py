"""Prompt construction for the advisor's news classifier and macro summarizer.

Both prompts are internal — never shown to the user, and carry NO dates or
timestamps (look-ahead hygiene). The hybrid advisor's only model-scored input is
the news read; the technical verdict is computed deterministically in
``rubric.py``, so no prompt ever sees the base rate, regime, or posture — which
is exactly what let the old free-form model rubber-stamp our own numbers.
"""

from __future__ import annotations

__all__ = ["build_news_prompt", "NEWS_JSON_SCHEMA", "build_macro_summary_prompt"]

# The hybrid path's ONLY model call: classify ticker news. No technical fields are
# in scope, so the model cannot re-score (and rubber-stamp) our own numbers.
NEWS_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "news_stance": {"type": "string",
                        "enum": ["supportive", "adverse", "neutral", "none"]},
        "severity": {"type": "string", "enum": ["none", "minor", "major"]},
        "material_news": {"type": "string"},
    },
    "required": ["news_stance", "severity", "material_news"],
}


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


def build_news_prompt(ticker: str, company_name: str, direction: str,
                      headlines: list[dict]) -> str:
    """Hybrid path — news-only classification for one already-fired entry.

    Carries NO technicals, base rate, or regime (the rubric owns those), and only
    catalyst headlines (price-action recaps are filtered upstream), so the model
    judges fresh company news, not our own numbers restated as headlines.
    """
    who = f"{company_name} ({ticker})" if company_name else ticker
    dir_txt = "long" if str(direction).lower() == "long" else "short"
    return (
        "You are a news analyst. A trader has ALREADY opened a "
        f"{dir_txt} position in {who} on technicals you cannot see and must not "
        "second-guess. Your ONLY job: judge whether recent company-specific NEWS "
        "supports or threatens that position.\n"
        "Rules: only news about THIS company counts — ignore broad market/macro "
        "commentary and other companies. Weigh news as fresh information, not the "
        "price move itself (a rally or drop is already in the technicals). If "
        "nothing here is material to the position, say so plainly.\n\n"
        "## HEADLINES\n"
        f"{_fmt_headlines(headlines, limit=6)}\n\n"
        "## INSTRUCTION\n"
        "Return a JSON object: news_stance (supportive|adverse|neutral|none — "
        "'none' when no headline carries material company news; 'supportive'/"
        f"'adverse' are relative to the {dir_txt} position), severity "
        "(none|minor|major — how strongly it bears on the entry), and "
        "material_news (one short clause naming the single most decision-relevant "
        "item, or empty)."
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
