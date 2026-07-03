"""Macro context builder — summarize headlines, fail-open to ''."""

from __future__ import annotations

from core.advisor import macro_context


def test_empty_headlines_skip_llm(monkeypatch):
    monkeypatch.setattr(macro_context, "fetch_macro_headlines", lambda **k: [])

    def _boom(*a, **k):
        raise AssertionError("LLM must not be called with no headlines")

    monkeypatch.setattr(macro_context, "ollama_chat", _boom)
    assert macro_context.build_market_context() == ""


def test_summary_returned_and_whitespace_collapsed(monkeypatch):
    monkeypatch.setattr(macro_context, "fetch_macro_headlines",
                        lambda **k: [{"headline": "CPI cools"}])
    monkeypatch.setattr(macro_context, "ollama_chat",
                        lambda *a, **k: "  Rates steady.\n Tech leads.  ")
    out = macro_context.build_market_context()
    assert out == "Rates steady. Tech leads."


def test_llm_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(macro_context, "fetch_macro_headlines",
                        lambda **k: [{"headline": "CPI cools"}])
    monkeypatch.setattr(macro_context, "ollama_chat", lambda *a, **k: None)
    assert macro_context.build_market_context() == ""


def test_summary_prompt_receives_headlines(monkeypatch):
    seen = {}
    monkeypatch.setattr(macro_context, "fetch_macro_headlines",
                        lambda **k: [{"headline": "Oil slips"}])

    def _capture(prompt, **k):
        seen["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(macro_context, "ollama_chat", _capture)
    macro_context.build_market_context()
    assert "Oil slips" in seen["prompt"]
