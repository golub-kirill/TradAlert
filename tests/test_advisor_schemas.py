"""AdvisorVerdict / AdvisorInput validation contract."""

from __future__ import annotations

import pytest

from core.advisor.schemas import AdvisorInput, AdvisorVerdict


@pytest.mark.parametrize("label", ["agree", "disagree", "flag"])
def test_valid_verdicts_accepted(label):
    v = AdvisorVerdict(verdict=label, confidence=0.5, reasoning="ok")
    assert v.verdict == label


def test_invalid_verdict_rejected():
    with pytest.raises(ValueError):
        AdvisorVerdict(verdict="maybe", confidence=0.5, reasoning="x")


@pytest.mark.parametrize("raw,expected", [(1.7, 1.0), (-0.4, 0.0), (0.5, 0.5)])
def test_confidence_clamped(raw, expected):
    assert AdvisorVerdict("agree", raw, "x").confidence == expected


def test_confidence_non_numeric_defaults_zero():
    assert AdvisorVerdict("agree", "not-a-number", "x").confidence == 0.0


def test_reasoning_and_risks_coerced_and_stripped():
    v = AdvisorVerdict("flag", 0.5, "  hi  ", risks=None)
    assert v.reasoning == "hi"
    assert v.risks == ""


def test_to_dict_roundtrip():
    v = AdvisorVerdict("disagree", 0.9, "weak setup", risks="earnings")
    assert v.to_dict() == {
        "verdict": "disagree",
        "confidence": 0.9,
        "reasoning": "weak setup",
        "risks": "earnings",
    }


def test_input_defaults_are_safe():
    inp = AdvisorInput(
        ticker="TEST.1", direction="long", signal_type="momentum",
        stop_price=95.0, target_price=110.0, min_rr=2.5,
        market_regime="BULL", ticker_trend="UPTREND", reason="r",
    )
    assert inp.headlines == [] and inp.market_context == ""
    assert inp.vix_level is None and inp.open_positions == 0


def test_input_excludes_lookahead_fields():
    # Look-ahead hygiene: no date/timestamp/entry-price field on the input DTO.
    fields = set(AdvisorInput.__dataclass_fields__)
    assert not (fields & {"date", "entry_date", "timestamp", "entry_price", "now"})
