"""Deterministic scoring rubric — the Python half of the hybrid advisor.

The LLM is a poor probability estimator: asked for a free ``confidence`` it
clusters everything in an 85–95% band and rubber-stamps our own numbers. So the
numeric verdict is computed here, from inputs the quant engine already produced
(base-rate edge, regime/trend alignment, overextension, liquidity, R:R, event
risk). Confidence is a fixed function of the score, not a vibe — a negative-
expectancy setup *cannot* score high. The LLM contributes only the news read
(``apply_news``), which can make the call more cautious but never inflate a weak
setup.

Weights live in ``WEIGHTS`` so the mapping stays calibratable against the live
journal (scripts/live/evaluate_advisor.py). Overextension is deliberately light:
overextension entry-vetoes were A/B-refuted (they tax the right tail), so it is a
flag to weigh, never a veto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.advisor.schemas import AdvisorInput

__all__ = ["Criterion", "Rubric", "score_rubric", "apply_news", "WEIGHTS"]

# Per-axis point ranges. Edge and alignment dominate; the rest are ±1 nudges.
WEIGHTS = {
    "edge_min_n": 20,          # base-rate cell below this n is treated as unknown
    "edge_strong": 0.15,       # avg_r >= → strong +3
    "edge_positive": 0.05,     # avg_r >= → positive +2
    "edge_negative": -0.05,    # avg_r <  → negative -3 (hard cap on confidence)
    "unknown_conf_cap": 0.70,  # no usable base rate → we lack the key input
    "conf_floor": 0.45,
    "conf_ceiling": 0.90,      # never claim near-certainty from technicals alone
    "conf_slope": 0.06,        # confidence gained per point of |score| toward the verdict
}


@dataclass
class Criterion:
    """One scored axis: a short label, its point contribution, and a note."""

    name: str
    label: str
    points: int
    note: str = ""


@dataclass
class Rubric:
    """The computed assessment before the news layer folds in."""

    criteria: list[Criterion] = field(default_factory=list)
    score: int = 0
    verdict: str = "flag"           # agree | disagree | flag (provisional)
    confidence: float = 0.5
    neg_edge: bool = False          # negative-expectancy base rate present
    edge_unknown: bool = False      # no usable base-rate cell
    reasoning: str = ""             # one-line technical summary (news-free)
    risk: str = ""                  # weakest axis, for the risks field

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "criteria": {c.name: {"label": c.label, "points": c.points} for c in self.criteria},
        }


def _edge(inp: AdvisorInput) -> Criterion:
    br = inp.base_rate or {}
    avg_r = br.get("avg_r")
    n = int(br.get("n", 0) or 0)
    if avg_r is None or n < WEIGHTS["edge_min_n"]:
        return Criterion("edge", "unknown", 0, "no base-rate cell")
    wr = br.get("win_rate")
    tag = f"{avg_r:+.2f}R" + (f"/{wr:.0%}" if wr is not None else "") + f" n={n}"
    if avg_r >= WEIGHTS["edge_strong"]:
        return Criterion("edge", "strong", 3, tag)
    if avg_r >= WEIGHTS["edge_positive"]:
        return Criterion("edge", "positive", 2, tag)
    if avg_r >= WEIGHTS["edge_negative"]:
        return Criterion("edge", "marginal", 0, tag)
    return Criterion("edge", "negative", -3, tag)


def _alignment(inp: AdvisorInput) -> Criterion:
    """Direction vs market regime + ticker trend. Aligned/counter carry ±2."""
    regime = (inp.market_regime or "").upper()
    trend = (inp.ticker_trend or "").upper()
    is_long = str(inp.direction).lower() == "long"
    if is_long:
        supportive = (regime in ("BULL", "NEUTRAL"), trend == "UPTREND")
        counter = (regime == "BEAR", trend == "DOWNTREND")
    else:
        supportive = (regime in ("BEAR", "NEUTRAL"), trend == "DOWNTREND")
        counter = (regime == "BULL", trend == "UPTREND")
    tag = f"{regime or '—'}/{trend or '—'}"
    if any(counter):
        return Criterion("alignment", "counter", -2, tag)
    if all(supportive):
        return Criterion("alignment", "aligned", 2, tag)
    return Criterion("alignment", "mixed", 0, tag)


def _overextension(inp: AdvisorInput) -> Criterion:
    """Stretched RSI / distance from MA / thin ATR buffer. A light flag only —
    overextension vetoes were A/B-refuted, so the worst it costs is -1."""
    is_long = str(inp.direction).lower() == "long"
    strong = mild = 0
    if inp.rsi is not None:
        if (is_long and inp.rsi > 78) or (not is_long and inp.rsi < 22):
            strong += 1
        elif (is_long and inp.rsi > 72) or (not is_long and inp.rsi < 28):
            mild += 1
    if inp.pct_from_ma is not None:
        ext = inp.pct_from_ma if is_long else -inp.pct_from_ma
        if ext > 15:
            strong += 1
        elif ext > 10:
            mild += 1
    if inp.atr_to_stop is not None:
        if inp.atr_to_stop < 0.5:
            strong += 1
        elif inp.atr_to_stop < 0.8:
            mild += 1
    if strong == 0 and mild == 0:
        if inp.rsi is None and inp.pct_from_ma is None and inp.atr_to_stop is None:
            return Criterion("overextension", "unknown", 0, "")
        return Criterion("overextension", "ok", 1, "")
    if strong >= 1 or mild >= 2:
        return Criterion("overextension", "extreme", -1, f"{strong+mild} flag(s)")
    return Criterion("overextension", "stretched", 0, f"{mild} flag")


def _liquidity(inp: AdvisorInput) -> Criterion:
    dv = inp.dv20
    cap = (inp.cap_tier or "").lower()
    if dv is None and not cap:
        return Criterion("liquidity", "unknown", 0, "")
    tag = (f"${dv/1e6:.1f}M/d" if dv is not None else "") + (f" {cap}-cap" if cap else "")
    if (dv is not None and dv >= 20e6) or cap in ("large", "mid"):
        return Criterion("liquidity", "ample", 1, tag.strip())
    if (dv is not None and dv < 3e6) or cap == "micro":
        return Criterion("liquidity", "illiquid", -1, tag.strip())
    return Criterion("liquidity", "thin", 0, tag.strip())


def _geometry(inp: AdvisorInput) -> Criterion:
    rr = inp.min_rr
    if rr is None:
        return Criterion("rr", "unknown", 0, "")
    tag = f"{rr:.1f}:1"
    if rr >= 2.5:
        return Criterion("rr", "strong", 1, tag)
    if rr >= 1.8:
        return Criterion("rr", "adequate", 0, tag)
    return Criterion("rr", "weak", -1, tag)


def _event(inp: AdvisorInput) -> Criterion:
    if inp.event_risk:
        return Criterion("event", "elevated", -1, str(inp.event_risk))
    return Criterion("event", "none", 0, "")


def _confidence(score: int, verdict: str, *, edge_unknown: bool) -> float:
    """Map |score toward the verdict| to a spread confidence, then apply caps."""
    if verdict == "flag":
        conf = 0.50 + WEIGHTS["conf_slope"] * min(abs(score), 3) * 0.5
    else:
        conf = WEIGHTS["conf_floor"] + WEIGHTS["conf_slope"] * min(abs(score), 8)
    conf = min(conf, WEIGHTS["conf_ceiling"])
    if edge_unknown:
        conf = min(conf, WEIGHTS["unknown_conf_cap"])
    return round(max(WEIGHTS["conf_floor"] if verdict != "flag" else 0.35, conf), 3)


def score_rubric(inp: AdvisorInput) -> Rubric:
    """Compute the technical rubric — verdict + calibrated confidence, news-free."""
    crits = [_edge(inp), _alignment(inp), _overextension(inp),
             _liquidity(inp), _geometry(inp), _event(inp)]
    score = sum(c.points for c in crits)
    edge = next(c for c in crits if c.name == "edge")
    neg_edge = edge.label == "negative"
    edge_unknown = edge.label == "unknown"

    if neg_edge:
        verdict = "disagree" if score <= -1 else "flag"
    elif score >= 2:
        verdict = "agree"
    elif score <= -2:
        verdict = "disagree"
    else:
        verdict = "flag"

    # A setup fighting its own regime/trend is never a clean 'agree', however
    # strong the other axes — a good edge can't paper over counter-alignment.
    align = next(c for c in crits if c.name == "alignment")
    if align.label == "counter" and verdict == "agree":
        verdict = "flag"

    conf = _confidence(score, verdict, edge_unknown=edge_unknown)

    # One-line technical summary — grounded, never the generic "momentum aligns
    # with bull regime" the free-form model kept emitting.
    edge_txt = {"strong": "strong edge", "positive": "positive edge",
                "marginal": "marginal edge", "negative": "NEGATIVE edge",
                "unknown": "edge unknown"}[edge.label]
    reasoning = f"{edge_txt} ({edge.note})" if edge.note else edge_txt
    reasoning += f"; {align.label} {align.note}"

    worst = min(crits, key=lambda c: c.points)
    risk = "" if worst.points >= 0 else f"{worst.name}: {worst.label} {worst.note}".strip()

    return Rubric(criteria=crits, score=score, verdict=verdict, confidence=conf,
                  neg_edge=neg_edge, edge_unknown=edge_unknown,
                  reasoning=reasoning, risk=risk)


def apply_news(rubric: Rubric, stance: str, severity: str, note: str = "") -> Rubric:
    """Fold the LLM news read into the rubric. News can only add caution — it
    downgrades or vetoes on adverse catalysts and penalizes when we are blind;
    it never rescues a negative-edge setup into high-confidence agreement.

    stance   : supportive | adverse | neutral | none | unknown
    severity : none | minor | major
    """
    stance = (stance or "unknown").lower()
    severity = (severity or "none").lower()
    r = rubric
    news_txt = note.strip()

    if stance == "adverse":
        if severity == "major":
            r.verdict = "disagree"
            r.confidence = round(max(r.confidence, 0.75), 3)
        else:  # minor adverse — never leave it at a clean 'agree'
            if r.verdict == "agree":
                r.verdict = "flag"
            r.confidence = round(min(r.confidence, 0.65), 3)
        r.risk = news_txt or r.risk or "adverse ticker news"
        r.reasoning += f"; adverse news: {news_txt}" if news_txt else "; adverse news"
    elif stance == "supportive":
        # A real catalyst can firm up a sound setup, but cannot override a bad
        # edge — the numbers still gate the ceiling.
        if r.verdict == "agree" and not r.neg_edge:
            r.confidence = round(min(r.confidence + 0.05, WEIGHTS["conf_ceiling"]), 3)
        if news_txt:
            r.reasoning += f"; supportive news: {news_txt}"
    elif stance in ("none", "unknown"):
        # No orthogonal news signal → we are partially blind; stay humble.
        r.confidence = round(min(r.confidence, WEIGHTS["unknown_conf_cap"]), 3)
        r.reasoning += "; no material news" if stance == "none" else "; news unread"
    # neutral → no change
    return r
