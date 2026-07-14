"""AI advisor: a live-only, non-blocking LLM second opinion on fired signals.

Advisory only — it never gates, sizes, or moves a stop, and the engine/backtester
never call it, so the backtest stays byte-identical. Every path is fail-open:
Ollama down, news APIs missing, or a malformed response all yield an empty note.
"""

from __future__ import annotations

from core.advisor.schemas import AdvisorInput, AdvisorVerdict, NewsRead, VerdictLabel
from core.advisor.service import (
    AdvisorContext,
    advise_signal,
    build_advisor_context,
    build_advisor_input,
    build_verdict,
)

__all__ = [
    "AdvisorInput",
    "AdvisorVerdict",
    "NewsRead",
    "VerdictLabel",
    "AdvisorContext",
    "build_advisor_context",
    "build_advisor_input",
    "build_verdict",
    "advise_signal",
]
