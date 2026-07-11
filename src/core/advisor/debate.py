"""Multi-agent debate: bull → bear → judge, fail-open with degrade to single-shot.

Each role is one grammar-constrained Ollama call. Any role returning None never
raises: a missing bull/bear still lets the judge rule on what exists; a missing
or malformed judge falls back to the single-shot verdict (``ask_llm``) on the
same enriched input; that failure yields ``None`` and the signal fires with no
note. A total wall-clock budget caps the extra latency the multi-call path adds
to a live scan — once exceeded, remaining roles are skipped and the ladder
degrades as if they had failed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from core.advisor.client import ask_json, ask_llm
from core.advisor.prompts import (
    BEAR_JSON_SCHEMA,
    BULL_JSON_SCHEMA,
    VERDICT_JSON_SCHEMA,
    build_bear_prompt,
    build_bull_prompt,
    build_judge_prompt,
)
from core.advisor.schemas import AdvisorInput, AdvisorVerdict, BearCase, BullCase

logger = logging.getLogger(__name__)

__all__ = ["DebateResult", "run_debate"]


@dataclass
class DebateResult:
    """Outcome of one debate — the final verdict plus the intermediate cases."""

    verdict: AdvisorVerdict | None = None
    bull: BullCase | None = None
    bear: BearCase | None = None
    fell_back: bool = False  # judge missing/malformed → single-shot verdict used


def _llm_kwargs(ctx) -> dict:
    return dict(
        endpoint=ctx.endpoint, model=ctx.model, timeout=ctx.timeout,
        temperature=ctx.temperature, max_tokens=ctx.max_tokens, session=ctx.session,
    )


def _bull_from(d: dict | None) -> BullCase | None:
    if not d:
        return None
    try:
        return BullCase(thesis=d.get("thesis", ""), points=d.get("points", []))
    except (TypeError, ValueError):
        return None


def _bear_from(d: dict | None) -> BearCase | None:
    if not d:
        return None
    try:
        return BearCase(thesis=d.get("thesis", ""), points=d.get("points", []),
                        rebuttal=d.get("rebuttal", ""))
    except (TypeError, ValueError):
        return None


def _verdict_from(d: dict | None) -> AdvisorVerdict | None:
    if not d:
        return None
    try:
        return AdvisorVerdict(
            verdict=str(d.get("verdict", "")).lower().strip(),
            confidence=d.get("confidence", 0.0),
            reasoning=d.get("reasoning", ""),
            risks=d.get("risks", ""),
        )
    except (TypeError, ValueError):
        return None


def run_debate(input_data: AdvisorInput, ctx) -> DebateResult:
    """Run bull → bear → judge for one fired entry. Never raises."""
    kw = _llm_kwargs(ctx)
    budget = float(getattr(ctx, "debate_total_timeout", 0) or 0)
    trichotomy = bool(getattr(ctx, "debate_risk_trichotomy", True))
    t0 = time.time()

    def over_budget() -> bool:
        return budget > 0 and (time.time() - t0) > budget

    res = DebateResult()
    ticker = input_data.ticker

    res.bull = _bull_from(ask_json(build_bull_prompt(ticker, input_data),
                                   BULL_JSON_SCHEMA, **kw))

    if not over_budget():
        res.bear = _bear_from(ask_json(build_bear_prompt(ticker, input_data, res.bull),
                                       BEAR_JSON_SCHEMA, **kw))

    if not over_budget():
        res.verdict = _verdict_from(ask_json(
            build_judge_prompt(ticker, input_data, res.bull, res.bear,
                               risk_trichotomy=trichotomy),
            VERDICT_JSON_SCHEMA, **kw))

    # Degrade: no usable judge verdict → single-shot on the same enriched input.
    if res.verdict is None:
        res.fell_back = True
        res.verdict = ask_llm(input_data, **kw)

    return res
