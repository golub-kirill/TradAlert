"""Typed inputs and outputs for the AI advisor.

``AdvisorInput`` is the signal context handed to the LLM. It deliberately
excludes dates, timestamps, and entry prices: the advisor is a live-only second
opinion, and leaking the decision date invites look-ahead reasoning that would
make any retrospective evaluation optimistic (see docs/AI_ADVISOR_PLAN.md E25).

``AdvisorVerdict`` is the parsed, validated response. Confidence is clamped to
[0, 1] and the verdict label is checked, so a malformed model response raises
here rather than reaching the render surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VerdictLabel = Literal["agree", "disagree", "flag"]

_VALID_VERDICTS: frozenset[str] = frozenset(("agree", "disagree", "flag"))

__all__ = ["AdvisorInput", "AdvisorVerdict", "VerdictLabel"]


@dataclass
class AdvisorInput:
    """Signal context + news passed to the LLM for advisory review.

    No dates/timestamps/entry-price fields by design (look-ahead hygiene). News
    headlines carry their own publication dates, but those are real-world facts,
    not the signal's decision date, so they cannot pin the model to a future bar.
    """

    ticker: str
    direction: str
    signal_type: str
    stop_price: float
    target_price: float
    min_rr: float
    market_regime: str
    ticker_trend: str
    reason: str
    tier: str = "LIVE"
    event_risk: str = ""
    vix_level: float | None = None
    macro_score: float | None = None
    behavioral_score: float | None = None
    open_positions: int = 0
    # News context (populated by the news layer)
    market_context: str = ""  # macro/sector paragraph
    headlines: list[dict] = field(default_factory=list)  # ticker-specific news


@dataclass
class AdvisorVerdict:
    """Parsed LLM response — the AI's opinion on a fired signal."""

    verdict: VerdictLabel
    confidence: float  # [0, 1]
    reasoning: str  # ~1-2 sentence rationale
    risks: str = ""  # optional risk note

    def __post_init__(self) -> None:
        if self.verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"verdict must be one of {sorted(_VALID_VERDICTS)}, got {self.verdict!r}"
            )
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.0
        self.reasoning = str(self.reasoning or "").strip()
        self.risks = str(self.risks or "").strip()

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "risks": self.risks,
        }
