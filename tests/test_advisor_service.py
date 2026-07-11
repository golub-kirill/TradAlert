"""Advisor orchestration — context build, note formatting, fail-open advise."""

from __future__ import annotations

from core.advisor import service
from core.advisor.schemas import AdvisorVerdict


class _Signal:
    direction = "long"
    signal_type = "momentum"
    stop_price = 95.0
    target_price = 110.0
    min_rr = 2.5
    market_regime = "BULL"
    ticker_trend = "UPTREND"
    reason = "breakout"
    tier = "LIVE"
    event_risk = "earnings in 2d"


# ── context build ────────────────────────────────────────────────────────────

def test_build_context_disabled_by_default():
    ctx = service.build_advisor_context({})
    assert ctx.enabled is False
    assert ctx.market_context == ""


def test_build_context_disabled_skips_macro(monkeypatch):
    monkeypatch.setattr(service, "build_market_context",
                        lambda **k: (_ for _ in ()).throw(AssertionError("no call")))
    ctx = service.build_advisor_context({"advisor": {"enabled": False}})
    assert ctx.enabled is False


def test_build_context_enabled_reads_config_and_env(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fk")
    monkeypatch.setenv("BRAVE_API_KEY", "bk")
    monkeypatch.setattr(service, "build_market_context", lambda **k: "macro ctx")
    ctx = service.build_advisor_context({
        "advisor": {"enabled": True, "model": "qwen3:8b", "timeout": 15},
        "news": {"cache_ttl_hours": 6, "max_headlines_per_ticker": 3},
    })
    assert ctx.enabled and ctx.model == "qwen3:8b" and ctx.timeout == 15
    assert ctx.cache_ttl_hours == 6 and ctx.max_headlines == 3
    assert ctx.finnhub_key == "fk" and ctx.brave_key == "bk"
    assert ctx.market_context == "macro ctx"


def test_build_context_macro_summarization_off(monkeypatch):
    monkeypatch.setattr(service, "build_market_context",
                        lambda **k: (_ for _ in ()).throw(AssertionError("no call")))
    ctx = service.build_advisor_context({
        "advisor": {"enabled": True},
        "news": {"macro_summarization": False},
    })
    assert ctx.enabled and ctx.market_context == ""


# ── note formatting ──────────────────────────────────────────────────────────

def test_format_note_verdict_confidence_and_risk():
    note = service.format_note(AdvisorVerdict("agree", 0.82, "strong momentum", risks="gap"))
    assert note.startswith("✅ Agree · 82% — strong momentum")
    assert "⚠ gap" in note


def test_format_note_truncated_to_column_width():
    long = AdvisorVerdict("flag", 0.5, "x" * 900)
    assert len(service.format_note(long)) <= 500


def test_format_note_clips_reasoning_at_word_boundary():
    # A long multi-word reasoning must not be cut mid-word — it ends with an ellipsis
    # and its last visible token is a whole word from the input.
    words = " ".join(f"word{i}" for i in range(200))
    note = service.format_note(AdvisorVerdict("disagree", 0.65, words))
    assert note.endswith("…")
    last_word = note[:-1].rstrip().rsplit(" ", 1)[-1]
    assert last_word in words.split()  # whole word, never a fragment


def test_format_note_clips_risks_at_word_boundary():
    note = service.format_note(
        AdvisorVerdict("disagree", 0.65, "short reason", risks="danger " * 60))
    risk_part = note.split("⚠", 1)[1]
    assert risk_part.rstrip().endswith("…")


def test_clip_short_text_unchanged():
    assert service._clip("all good here", 100) == "all good here"


# ── advise_signal ────────────────────────────────────────────────────────────

def test_advise_disabled_returns_empty():
    ctx = service.AdvisorContext(enabled=False)
    assert service.advise_signal("AAPL", _Signal(), ctx) == ""


def test_advise_enabled_returns_note(monkeypatch):
    monkeypatch.setattr(service, "load_fresh_news", lambda *a, **k: [{"headline": "h"}])
    monkeypatch.setattr(service, "ask_llm",
                        lambda inp, **k: AdvisorVerdict("agree", 0.7, "ok"))
    ctx = service.AdvisorContext(enabled=True)
    note = service.advise_signal("AAPL", _Signal(), ctx, vix_level=14.0)
    assert note.startswith("✅ Agree · 70% — ok")


def test_advise_passes_company_name_to_llm(monkeypatch):
    # Regression: a bare ticker made the model misread name-based news as an
    # identity mismatch (ARX.TO news names "ARC Resources"). The resolved company
    # name must reach the LLM input.
    seen = {}
    monkeypatch.setattr(service, "load_fresh_news", lambda *a, **k: [{"headline": "h"}])

    def _capture(inp, **k):
        seen["company_name"] = inp.company_name
        return AdvisorVerdict("agree", 0.7, "ok")

    monkeypatch.setattr(service, "ask_llm", _capture)
    ctx = service.AdvisorContext(enabled=True,
                                 company_names={"ARX.TO": "ARC Resources Ltd."})
    service.advise_signal("ARX.TO", _Signal(), ctx)
    assert seen["company_name"] == "ARC Resources Ltd."


def test_advise_missing_company_name_is_blank(monkeypatch):
    seen = {}
    monkeypatch.setattr(service, "load_fresh_news", lambda *a, **k: [])
    monkeypatch.setattr(service, "fetch_ticker_news", lambda *a, **k: [{"headline": "h"}])

    def _capture(inp, **k):
        seen["company_name"] = inp.company_name
        return AdvisorVerdict("agree", 0.7, "ok")

    monkeypatch.setattr(service, "ask_llm", _capture)
    monkeypatch.setattr(service, "save_news", lambda *a, **k: None)
    ctx = service.AdvisorContext(enabled=True, company_names={})
    service.advise_signal("ZZZZ", _Signal(), ctx)
    assert seen["company_name"] == ""


def test_advise_none_verdict_returns_empty(monkeypatch):
    monkeypatch.setattr(service, "load_fresh_news", lambda *a, **k: [])
    monkeypatch.setattr(service, "fetch_ticker_news", lambda *a, **k: [])
    monkeypatch.setattr(service, "search_ticker_news", lambda *a, **k: [])
    monkeypatch.setattr(service, "ask_llm", lambda inp, **k: None)
    ctx = service.AdvisorContext(enabled=True)
    assert service.advise_signal("AAPL", _Signal(), ctx) == ""


def test_advise_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(service, "load_fresh_news",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx = service.AdvisorContext(enabled=True)
    assert service.advise_signal("AAPL", _Signal(), ctx) == ""


# ── build_advisor_input ──────────────────────────────────────────────────────

class _Scan:
    close = 100.0
    atr = 2.0
    atr_pct = 2.0
    dv20 = 5_000_000.0
    market_cap = 50e9
    rsi = 61.0


def test_build_input_posture_from_scan(monkeypatch):
    monkeypatch.setattr(service, "_resolve_headlines", lambda *a, **k: [])
    ctx = service.AdvisorContext(enabled=True)
    inp = service.build_advisor_input(
        "AAPL", _Signal(), ctx, scan=_Scan(), pct_from_ma=7.5, rp_rank=88.0)
    assert inp.rsi == 61.0 and inp.atr_pct == 2.0
    assert inp.dv20 == 5_000_000.0 and inp.market_cap == 50e9
    assert inp.cap_tier == "large"
    assert inp.pct_from_ma == 7.5 and inp.rp_rank == 88.0
    assert inp.atr_to_stop == 2.5  # |100 - 95| / 2


def test_build_input_no_scan_posture_none(monkeypatch):
    monkeypatch.setattr(service, "_resolve_headlines", lambda *a, **k: [])
    ctx = service.AdvisorContext(enabled=True)
    inp = service.build_advisor_input("AAPL", _Signal(), ctx)
    assert inp.rsi is None and inp.atr_pct is None and inp.dv20 is None
    assert inp.market_cap is None and inp.cap_tier == ""
    assert inp.atr_to_stop is None and inp.pct_from_ma is None and inp.rp_rank is None
    assert inp.min_rr == 2.5 and inp.stop_price == 95.0  # geometry intact


def test_cap_tier_buckets():
    assert service._cap_tier(None) == ""
    assert service._cap_tier(50e9) == "large"
    assert service._cap_tier(5e9) == "mid"
    assert service._cap_tier(1e9) == "small"
    assert service._cap_tier(1e8) == "micro"


def test_sfloat_safe():
    assert service._sfloat(None) is None
    assert service._sfloat("x") is None
    assert service._sfloat("3.5") == 3.5 and service._sfloat(4) == 4.0


# ── headline resolution chain ────────────────────────────────────────────────

def test_resolve_headlines_prefers_cache(monkeypatch):
    monkeypatch.setattr(service, "load_fresh_news", lambda *a, **k: [{"headline": "cached"}])
    monkeypatch.setattr(service, "fetch_ticker_news",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    ctx = service.AdvisorContext(enabled=True)
    assert service._resolve_headlines("AAPL", ctx) == [{"headline": "cached"}]


def test_resolve_headlines_falls_through_to_brave(monkeypatch):
    saved = []
    monkeypatch.setattr(service, "load_fresh_news", lambda *a, **k: [])
    monkeypatch.setattr(service, "fetch_ticker_news", lambda *a, **k: [])
    monkeypatch.setattr(service, "search_ticker_news", lambda *a, **k: [{"headline": "brave"}])
    monkeypatch.setattr(service, "save_news", lambda *a, **k: saved.append(a))
    ctx = service.AdvisorContext(enabled=True)
    assert service._resolve_headlines("AAPL", ctx) == [{"headline": "brave"}]
    assert saved and saved[0][1] == "search"  # cached under the search section
