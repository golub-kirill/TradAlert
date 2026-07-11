"""Multi-agent debate — happy path + fail-open degrade ladder."""

from __future__ import annotations

import time

import pytest

from core.advisor import debate
from core.advisor.schemas import AdvisorInput, AdvisorVerdict, BearCase, BullCase


class _Ctx:
    endpoint = "http://x"
    model = "m"
    timeout = 5
    temperature = 0.1
    max_tokens = 300
    session = None
    debate_total_timeout = 0.0
    debate_risk_trichotomy = True


def _inp() -> AdvisorInput:
    return AdvisorInput(ticker="AAPL", direction="long", signal_type="momentum",
                        stop_price=95.0, target_price=110.0, min_rr=2.5,
                        market_regime="BULL", ticker_trend="UPTREND", reason="breakout")


def _role(schema: dict) -> str:
    """Identify which debate role a schema belongs to by its properties."""
    props = schema["properties"]
    if "verdict" in props:
        return "judge"
    if "rebuttal" in props:
        return "bear"
    return "bull"


def test_run_debate_happy_path(monkeypatch):
    seen = []

    def fake_ask_json(prompt, schema, **k):
        role = _role(schema)
        seen.append(role)
        return {
            "bull": {"thesis": "bull t", "points": ["p1", "p2"]},
            "bear": {"thesis": "bear t", "points": ["r1"], "rebuttal": "reb"},
            "judge": {"verdict": "agree", "confidence": 0.8, "reasoning": "ok", "risks": ""},
        }[role]

    monkeypatch.setattr(debate, "ask_json", fake_ask_json)
    monkeypatch.setattr(debate, "ask_llm", lambda *a, **k: pytest.fail("no fallback"))
    res = debate.run_debate(_inp(), _Ctx())
    assert seen == ["bull", "bear", "judge"]
    assert res.verdict.verdict == "agree" and res.verdict.confidence == 0.8
    assert isinstance(res.bull, BullCase) and res.bull.thesis == "bull t"
    assert isinstance(res.bear, BearCase) and res.bear.rebuttal == "reb"
    assert res.fell_back is False


def test_run_debate_judge_fails_falls_back(monkeypatch):
    def fake_ask_json(prompt, schema, **k):
        if _role(schema) == "judge":
            return None
        return {"thesis": "t", "points": [], "rebuttal": ""}

    monkeypatch.setattr(debate, "ask_json", fake_ask_json)
    monkeypatch.setattr(debate, "ask_llm",
                        lambda inp, **k: AdvisorVerdict("flag", 0.4, "single-shot"))
    res = debate.run_debate(_inp(), _Ctx())
    assert res.fell_back is True and res.verdict.reasoning == "single-shot"


def test_run_debate_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(debate, "ask_json", lambda *a, **k: None)
    monkeypatch.setattr(debate, "ask_llm", lambda *a, **k: None)
    res = debate.run_debate(_inp(), _Ctx())
    assert res.verdict is None and res.fell_back is True
    assert res.bull is None and res.bear is None


def test_run_debate_bull_none_still_judges(monkeypatch):
    def fake_ask_json(prompt, schema, **k):
        role = _role(schema)
        if role == "bull":
            return None
        if role == "bear":
            return {"thesis": "bear", "points": ["p"], "rebuttal": ""}
        return {"verdict": "disagree", "confidence": 0.6, "reasoning": "r", "risks": "x"}

    monkeypatch.setattr(debate, "ask_json", fake_ask_json)
    monkeypatch.setattr(debate, "ask_llm", lambda *a, **k: pytest.fail("no fallback"))
    res = debate.run_debate(_inp(), _Ctx())
    assert res.bull is None and isinstance(res.bear, BearCase)
    assert res.verdict.verdict == "disagree" and res.fell_back is False


def test_run_debate_budget_skips_after_bull(monkeypatch):
    calls = {"n": 0}

    def fake_ask_json(prompt, schema, **k):
        calls["n"] += 1
        time.sleep(0.02)
        return {"thesis": "a", "points": []}

    ctx = _Ctx()
    ctx.debate_total_timeout = 0.001  # already exceeded after the first call
    monkeypatch.setattr(debate, "ask_json", fake_ask_json)
    monkeypatch.setattr(debate, "ask_llm",
                        lambda *a, **k: AdvisorVerdict("agree", 0.5, "ss"))
    res = debate.run_debate(_inp(), ctx)
    assert calls["n"] == 1  # bear + judge skipped by the wall-clock budget
    assert res.fell_back is True and res.verdict.reasoning == "ss"
