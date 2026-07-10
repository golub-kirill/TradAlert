"""Advisor prompt contract — required fields, no dates, JSON instruction."""

from __future__ import annotations

import re

from core.advisor.prompts import (
    VERDICT_JSON_SCHEMA,
    build_macro_summary_prompt,
    build_prompt,
)
from core.advisor.schemas import AdvisorInput


def _input(**over) -> AdvisorInput:
    base = dict(
        ticker="AAPL", direction="long", signal_type="momentum",
        stop_price=182.5, target_price=205.0, min_rr=2.5,
        market_regime="BULL_NORMAL", ticker_trend="UPTREND",
        reason="close above 20d high", event_risk="earnings in 2d",
        vix_level=14.2, macro_score=0.6, behavioral_score=0.35, open_positions=3,
    )
    base.update(over)
    return AdvisorInput(**base)


def test_prompt_includes_all_required_fields():
    p = build_prompt("AAPL", _input())
    for token in ("AAPL", "long", "momentum", "2.50:1", "BULL_NORMAL",
                  "UPTREND", "14.20", "0.60", "0.35", "earnings in 2d"):
        assert token in p, token


def test_prompt_requests_json_verdict():
    p = build_prompt("AAPL", _input())
    assert "JSON" in p
    assert "agree" in p and "disagree" in p and "flag" in p


def test_prompt_handles_none_values():
    p = build_prompt("AAPL", _input(vix_level=None, macro_score=None,
                                    behavioral_score=None, event_risk=""))
    assert "VIX Level: —" in p
    assert "Macro Score: —" in p
    assert "Event Risk: —" in p


def test_prompt_contains_no_iso_dates():
    # Look-ahead hygiene: no YYYY-MM-DD in the prompt body.
    p = build_prompt("AAPL", _input(reason="setup", event_risk="earnings soon"))
    assert not re.search(r"\d{4}-\d{2}-\d{2}", p)


def test_prompt_includes_headlines_when_present():
    p = build_prompt("AAPL", _input(headlines=[
        {"headline": "Buyback expanded", "source": "Reuters"},
        {"headline": "Peer profit warning", "source": "Bloomberg"},
    ]))
    assert "Buyback expanded" in p and "Reuters" in p
    assert "Peer profit warning" in p


def test_prompt_skips_headlines_when_absent():
    p = build_prompt("AAPL", _input(headlines=[]))
    assert "## TICKER NEWS\n—" in p


def test_prompt_includes_company_name_when_present():
    p = build_prompt("ARX.TO", _input(ticker="ARX.TO", company_name="ARC Resources Ltd."))
    assert "ARC Resources Ltd." in p
    assert "ARX.TO" in p


def test_prompt_omits_company_when_absent():
    p = build_prompt("AAPL", _input(company_name=""))
    assert "(company:" not in p


def test_prompt_has_identity_mismatch_guard():
    # The model must not flag a name-vs-symbol difference as an asset mismatch.
    p = build_prompt("ARX.TO", _input(ticker="ARX.TO", company_name="ARC Resources Ltd."))
    assert "identity" in p.lower() and "mismatch" in p.lower()


def test_prompt_includes_market_context():
    p = build_prompt("AAPL", _input(market_context="Rates steady, tech leads."))
    assert "Rates steady, tech leads." in p


def test_macro_summary_prompt_lists_headlines():
    p = build_macro_summary_prompt([{"headline": "CPI cools"}, {"headline": "Oil slips"}])
    assert "CPI cools" in p and "Oil slips" in p
    assert "2-3 sentence" in p


def test_verdict_schema_shape():
    props = VERDICT_JSON_SCHEMA["properties"]
    assert props["verdict"]["enum"] == ["agree", "disagree", "flag"]
    assert set(VERDICT_JSON_SCHEMA["required"]) == {
        "verdict", "confidence", "reasoning", "risks"}
