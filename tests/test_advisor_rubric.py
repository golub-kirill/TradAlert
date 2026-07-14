"""Deterministic rubric — the computed verdict/confidence must separate setup
quality and never let news inflate a weak edge."""

from __future__ import annotations

from core.advisor.rubric import apply_news, score_rubric
from core.advisor.schemas import AdvisorInput


def _inp(**over) -> AdvisorInput:
    base = dict(
        ticker="TEST.1", company_name="Test Industries Inc.", direction="long",
        signal_type="momentum", stop_price=90.0, target_price=115.0, min_rr=2.0,
        market_regime="BULL", ticker_trend="UPTREND", reason="breakout",
        rsi=64.0, pct_from_ma=6.2, atr_to_stop=1.4, dv20=52e6, cap_tier="large",
        base_rate={"avg_r": 0.35, "win_rate": 0.58, "n": 140},
    )
    base.update(over)
    return AdvisorInput(**base)


def test_clean_positive_edge_agrees():
    r = score_rubric(_inp())
    assert r.verdict == "agree" and r.confidence >= 0.8


def test_negative_edge_is_never_high_confidence_agree():
    # The core defect being fixed: a -EV base rate on an otherwise clean setup
    # used to score 'agree ~88%'. It must not agree, and confidence must be low.
    r = score_rubric(_inp(base_rate={"avg_r": -0.28, "win_rate": 0.34, "n": 110}))
    assert r.verdict != "agree"
    assert r.neg_edge and r.confidence < 0.65


def test_strongly_negative_edge_disagrees():
    r = score_rubric(_inp(base_rate={"avg_r": -0.30, "win_rate": 0.33, "n": 120},
                          dv20=2e6, cap_tier="micro"))
    assert r.verdict == "disagree"


def test_counter_trend_is_not_a_clean_agree():
    r = score_rubric(_inp(market_regime="BEAR", ticker_trend="DOWNTREND"))
    assert r.verdict != "agree"


def test_unknown_edge_caps_confidence():
    r = score_rubric(_inp(base_rate={}))
    assert r.edge_unknown and r.confidence <= 0.70


def test_confidence_spreads_across_quality():
    strong = score_rubric(_inp()).confidence
    weak = score_rubric(_inp(base_rate={"avg_r": 0.0, "win_rate": 0.5, "n": 100})).confidence
    assert strong > weak  # not the old flat 85-95% band


def test_news_adverse_major_forces_disagree():
    r = apply_news(score_rubric(_inp()), "adverse", "major", "guidance cut")
    assert r.verdict == "disagree" and "guidance cut" in r.risk


def test_news_minor_adverse_downgrades_agree_to_flag():
    r = apply_news(score_rubric(_inp()), "adverse", "minor", "minor recall")
    assert r.verdict == "flag"


def test_supportive_news_cannot_rescue_negative_edge():
    base = score_rubric(_inp(base_rate={"avg_r": -0.28, "win_rate": 0.34, "n": 110}))
    r = apply_news(base, "supportive", "major", "$2B contract")
    assert r.verdict != "agree"  # edge still gates the ceiling


def test_news_none_penalizes_confidence():
    clean = score_rubric(_inp())
    penalized = apply_news(score_rubric(_inp()), "none", "none", "")
    assert penalized.confidence <= clean.confidence
