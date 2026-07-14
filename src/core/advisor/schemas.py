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
_VALID_STANCES: frozenset[str] = frozenset(("supportive", "adverse", "neutral", "none", "unknown"))

__all__ = ["AdvisorInput", "AdvisorVerdict", "VerdictLabel", "NewsRead"]


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
    # Full company name (e.g. "ARC Resources Ltd." for ARX.TO). Lets the model map
    # symbol → issuer so it doesn't misread name-based news as an identity mismatch.
    company_name: str = ""
    tier: str = "LIVE"
    event_risk: str = ""
    vix_level: float | None = None
    macro_score: float | None = None
    behavioral_score: float | None = None
    open_positions: int = 0
    # News context (populated by the news layer)
    market_context: str = ""  # macro/sector paragraph
    headlines: list[dict] = field(default_factory=list)  # ticker-specific news
    # Technical posture (last-bar snapshot; None when unavailable). All are
    # relative measures — no absolute entry price — so look-ahead hygiene holds.
    rsi: float | None = None
    atr_pct: float | None = None          # atr / close × 100
    pct_from_ma: float | None = None      # (close − ma_slow) / ma_slow × 100
    atr_to_stop: float | None = None      # |close − stop| / atr (risk in ATRs)
    dv20: float | None = None             # 20-day average dollar volume (liquidity)
    market_cap: float | None = None
    cap_tier: str = ""                    # large | mid | small | micro | ""
    rp_rank: float | None = None          # relative-position rank (location, 0–100)
    # Historical edge for this setup, precomputed over resolved trades only
    # (aggregate — no per-trade outcome leaks). {n, win_rate, avg_r, expectancy}.
    base_rate: dict = field(default_factory=dict)


@dataclass
class AdvisorVerdict:
    """The advisor's opinion on a fired signal.

    The verdict + confidence are computed by the rubric (deterministic, from
    quant inputs) and the LLM contributes only the news read; ``rubric`` carries
    the per-axis breakdown for cards/journaling and ``news_stance`` the
    classified stance.
    """

    verdict: VerdictLabel
    confidence: float  # [0, 1]
    reasoning: str  # ~1-2 sentence rationale
    risks: str = ""  # optional risk note
    rubric: dict = field(default_factory=dict)   # per-axis score breakdown
    news_stance: str = ""                          # supportive|adverse|neutral|none|unknown

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
            "rubric": self.rubric,
            "news_stance": self.news_stance,
        }


@dataclass
class NewsRead:
    """The LLM's news-only classification — the sole model input to the verdict.

    Deliberately narrow: the model judges ticker news novelty and direction, not
    the technicals (those are the rubric's job), so it cannot rubber-stamp our
    own numbers.
    """

    stance: str = "unknown"          # supportive | adverse | neutral | none | unknown
    severity: str = "none"           # none | minor | major
    material_news: str = ""          # one-line most decision-relevant catalyst

    def __post_init__(self) -> None:
        self.stance = str(self.stance or "unknown").lower().strip()
        if self.stance not in _VALID_STANCES:
            self.stance = "unknown"
        self.severity = str(self.severity or "none").lower().strip()
        if self.severity not in ("none", "minor", "major"):
            self.severity = "none"
        self.material_news = str(self.material_news or "").strip()
