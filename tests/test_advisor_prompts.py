"""Advisor prompt contract — the news classifier carries only news, never our
own technical numbers (that is what let the free-form model rubber-stamp them)."""

from __future__ import annotations

import re

from core.advisor.prompts import (
    NEWS_JSON_SCHEMA,
    build_macro_summary_prompt,
    build_news_prompt,
)

_HEADS = [{"headline": "Buyback expanded", "source": "Reuters"},
          {"headline": "Guidance cut on weak demand", "source": "Bloomberg"}]


def test_news_prompt_includes_company_ticker_direction_and_headlines():
    p = build_news_prompt("ARX.TO", "ARC Resources Ltd.", "long", _HEADS)
    assert "ARC Resources Ltd." in p and "ARX.TO" in p
    assert "long" in p
    assert "Buyback expanded" in p and "Guidance cut on weak demand" in p


def test_news_prompt_carries_no_technical_inputs():
    # The whole point of the hybrid split: the model must not see the base rate,
    # regime, posture, or R:R — only news. Guard against regressions.
    p = build_news_prompt("AAPL", "Apple Inc.", "long", _HEADS).lower()
    for leaked in ("base rate", "win rate", "regime", "rsi", "atr", "r:r",
                   "risk/reward", "posture", "expectancy", "confidence"):
        assert leaked not in p, leaked


def test_news_prompt_requests_json_stance():
    p = build_news_prompt("AAPL", "Apple Inc.", "long", _HEADS)
    assert "JSON" in p
    assert "news_stance" in p and "severity" in p and "material_news" in p


def test_news_prompt_no_headlines_placeholder():
    p = build_news_prompt("AAPL", "Apple Inc.", "long", [])
    assert "## HEADLINES\n—" in p


def test_news_prompt_omits_company_parens_when_absent():
    p = build_news_prompt("AAPL", "", "long", _HEADS)
    assert "(company:" not in p and "()" not in p


def test_news_prompt_contains_no_iso_dates():
    # Look-ahead hygiene: no YYYY-MM-DD in the prompt body.
    p = build_news_prompt("AAPL", "Apple Inc.", "short",
                          [{"headline": "Recall announced", "source": "AP"}])
    assert not re.search(r"\d{4}-\d{2}-\d{2}", p)


def test_macro_summary_prompt_lists_headlines():
    p = build_macro_summary_prompt([{"headline": "CPI cools"}, {"headline": "Oil slips"}])
    assert "CPI cools" in p and "Oil slips" in p
    assert "2-3 sentence" in p


def test_news_schema_shape():
    props = NEWS_JSON_SCHEMA["properties"]
    assert props["news_stance"]["enum"] == ["supportive", "adverse", "neutral", "none"]
    assert props["severity"]["enum"] == ["none", "minor", "major"]
    assert set(NEWS_JSON_SCHEMA["required"]) == {"news_stance", "severity", "material_news"}
