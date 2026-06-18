"""Characterization test for main._run_pipeline.

Pins the OBSERVABLE behavior of the per-ticker live-scan loop — which input
condition yields which TickerResult, and where chart()/max-hold/breakeven are
invoked — so the loop can be decomposed into helpers without changing behavior.
Every external dependency is mocked (no cache, network, DB, or render), and one
pass over a curated ticker list exercises every branch.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from core.filter_engine import MarketRegime, ScanResult, SignalResult
from exceptions import InsufficientDataError

MA_SLOW = 10


def _df(branch: str, n: int = 12) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "volume": 1_000_000.0, "_branch": branch},
        index=idx,
    )


# Per-ticker OHLCV the mocked cache_load hands back; "_branch" marks the branch.
_DFS = {
    "BADIND": _df("BADIND"),
    "FEWROWS": _df("ok", n=5),
    "WARMUP": _df("WARMUP"),
    "SCANRAISE": _df("ok"),
    "FILTERED": _df("ok"),
    "INSUFF": _df("ok"),
    "SIGRAISE": _df("ok"),
    "ENTRY": _df("ok"),
    "HELD": _df("ok"),
}


class _StubEngine:
    """Minimal FilterEngine stand-in driving each ticker down one branch."""

    def __init__(self):
        self._today = None
        self.cfg = SimpleNamespace(
            trend=SimpleNamespace(ma_slow=MA_SLOW),
            execution=SimpleNamespace(max_hold_days=25, max_hold_mode="if_not_profit"),
        )

    def scan(self, ticker, df, market_cap=None):
        if ticker == "SCANRAISE":
            raise RuntimeError("scan boom")
        if ticker == "FILTERED":
            return ScanResult(passed=False, reason="filtered")
        return ScanResult(passed=True, reason="ok")

    def market_regime(self, market_dfs, vix_df, empty_vote_trend="BULL"):
        return MarketRegime(trend="BULL", volatility="NORMAL")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None, earnings_date=None,
               held_long=False, held_short=False, regime=None, with_checks=False):
        if ticker == "INSUFF":
            raise InsufficientDataError(got=42, need=100)
        if ticker == "SIGRAISE":
            raise RuntimeError("signal boom")
        if held_long or held_short:
            return SignalResult(passed=False, reason="hold")
        return SignalResult(passed=True, direction="long", signal_type="momentum",
                            stop_price=95.0, target_price=120.0,
                            market_regime="BULL_NORMAL", ticker_trend="UPTREND",
                            reason="fire")


@pytest.fixture
def pipeline(monkeypatch):
    calls = {"chart": [], "maxhold": [], "breakeven": []}

    def _cache_load(ticker):
        if ticker == "BADCACHE":
            raise RuntimeError("cache boom")
        return _DFS[ticker]

    def _attach(df):
        if df["_branch"].iloc[0] == "BADIND":
            raise RuntimeError("indicator boom")
        return df

    monkeypatch.setattr(main, "cache_load", _cache_load)
    monkeypatch.setattr(main, "_attach_indicators", _attach)
    monkeypatch.setattr(main, "_indicators_ready",
                        lambda df: df["_branch"].iloc[0] != "WARMUP")
    monkeypatch.setattr(main, "get_market_cap", lambda t: 1e9)
    monkeypatch.setattr(main, "get_next_earnings", lambda t: None)
    monkeypatch.setattr(main, "_load_market_context",
                        lambda tickers, now=None: ({"SPY": _df("ok"), "QQQ": _df("ok")}, _df("ok")))
    monkeypatch.setattr(main, "load_open_positions",
                        lambda: {"HELD": SimpleNamespace(
                            id=1, side="long", entry_price=100.0,
                            entry_date=date(2025, 1, 1), stop_price=95.0,
                            initial_stop=95.0)})
    monkeypatch.setattr(main, "_expected_hold_range", lambda engine: (5, 25))
    monkeypatch.setattr(main, "_append_live_context_checks",
                        lambda *a, **k: None)
    monkeypatch.setattr(main, "chart",
                        lambda ticker, *a, **k: calls["chart"].append(ticker))
    monkeypatch.setattr(main, "max_hold_exit_due",
                        lambda **k: calls["maxhold"].append(k) or False)
    monkeypatch.setattr(main, "_maybe_raise_stop_to_breakeven",
                        lambda ticker, *a, **k: calls["breakeven"].append(ticker))
    # Freshness guard: no network — refetch is a no-op (can't freshen), live price absent.
    monkeypatch.setattr(main, "get_or_fetch", lambda *a, **k: None)
    monkeypatch.setattr(main, "get_live_price", lambda *a, **k: None)
    return calls


def test_run_pipeline_branches(pipeline):
    tickers = ["^VIX", "BADCACHE", "BADIND", "FEWROWS", "WARMUP", "SCANRAISE",
               "FILTERED", "INSUFF", "SIGRAISE", "ENTRY", "HELD"]
    # Inject `now` = post-close of the fixtures' last bar (2025-01-16) so the freshness guard
    # reads the 12-row fixtures as fresh (LIVE) rather than stale vs the real wall clock.
    now = datetime(2025, 1, 16, 22, 0, tzinfo=timezone.utc)
    results = main._run_pipeline(tickers, _StubEngine(), settings={}, now=now)
    by = {r.ticker: r for r in results}

    # ^VIX is context-only → no result; every other ticker yields exactly one.
    assert "^VIX" not in by
    assert len(results) == 10

    assert by["BADCACHE"].scan.passed is False
    assert by["BADCACHE"].scan.reason == "cache load failed"
    assert by["BADCACHE"].error == "cache boom"

    assert by["BADIND"].scan.reason == "indicator error"
    assert by["BADIND"].error == "indicator boom"

    assert by["FEWROWS"].scan.reason == "only 5 rows — need 10 for scan"
    assert by["FEWROWS"].signal is None

    assert by["WARMUP"].scan.reason == "indicators still in warmup (NaN on last bar)"

    assert by["SCANRAISE"].scan.reason == "scan exception"
    assert by["SCANRAISE"].error == "scan boom"

    # scan filtered, not held → scan returned verbatim, no signal evaluated.
    assert by["FILTERED"].scan.passed is False
    assert by["FILTERED"].scan.reason == "filtered"
    assert by["FILTERED"].signal is None

    assert by["INSUFF"].signal is not None
    assert by["INSUFF"].signal.passed is False
    assert by["INSUFF"].signal.reason == "insufficient data: need at least 100 rows, got 42"

    assert by["SIGRAISE"].error == "signal boom"
    assert by["SIGRAISE"].signal is None

    # entry fires → charted, signal carried through.
    assert by["ENTRY"].signal.passed is True
    assert by["ENTRY"].signal.direction == "long"
    assert pipeline["chart"] == ["ENTRY"]  # charted iff signal.passed

    # held long → max-hold (7b) + breakeven (7c) both consulted; not charted (hold).
    assert by["HELD"].signal.passed is False
    assert "HELD" in pipeline["breakeven"]
    assert len(pipeline["maxhold"]) == 1  # only the held ticker reaches 7b


def test_run_pipeline_stamps_event_risk(pipeline):
    """A scan-wide upcoming macro event is stamped onto every fresh entry's signal."""
    from core.macro.calendar import CalendarEvent
    now = datetime(2025, 1, 16, 22, 0, tzinfo=timezone.utc)        # post-close of the last bar
    events = [CalendarEvent(date(2025, 1, 18), "FOMC", "FOMC decision day")]
    results = main._run_pipeline(["ENTRY"], _StubEngine(), settings={}, now=now,
                                 cal_events=events)
    by = {r.ticker: r for r in results}
    assert by["ENTRY"].signal.event_risk == "FOMC in 2d (2025-01-18)"


def test_run_pipeline_no_event_risk_without_calendar(pipeline):
    """No cal_events → the advisory stays empty (and the backtest is unaffected anyway)."""
    now = datetime(2025, 1, 16, 22, 0, tzinfo=timezone.utc)
    results = main._run_pipeline(["ENTRY"], _StubEngine(), settings={}, now=now)
    by = {r.ticker: r for r in results}
    assert by["ENTRY"].signal.event_risk == ""


def test_run_pipeline_event_risk_respects_settings_window(pipeline):
    """An out-of-window event (8d out, window 5) leaves the advisory empty."""
    from core.macro.calendar import CalendarEvent
    now = datetime(2025, 1, 16, 22, 0, tzinfo=timezone.utc)
    events = [CalendarEvent(date(2025, 1, 24), "CPI", "CPI release")]   # 8 days out
    results = main._run_pipeline(
        ["ENTRY"], _StubEngine(),
        settings={"scanner": {"event_risk_within_days": 5}}, now=now, cal_events=events)
    by = {r.ticker: r for r in results}
    assert by["ENTRY"].signal.event_risk == ""


def test_run_pipeline_empty_when_only_context():
    # No engine work, no positions: a context-only universe yields no results.
    import main as _m

    class _E:
        cfg = SimpleNamespace(
            trend=SimpleNamespace(ma_slow=MA_SLOW),
            execution=SimpleNamespace(max_hold_days=None, max_hold_mode="hard"))

    saved = (_m._load_market_context, _m.load_open_positions, _m._expected_hold_range)
    _m._load_market_context = lambda tickers, now=None: (None, None)
    _m.load_open_positions = lambda: {}
    _m._expected_hold_range = lambda engine: (5, 25)
    try:
        out = _m._run_pipeline(["^VIX"], _E(), settings={})
    finally:
        (_m._load_market_context, _m.load_open_positions, _m._expected_hold_range) = saved
    assert out == []
