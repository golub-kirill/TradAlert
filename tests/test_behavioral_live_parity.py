"""Live==backtest parity for the behavioral sizing axis.

``breadth_divergence`` only fires when the classifier is handed the SPY frame.
The backtester passes it (``portfolio_backtester`` → ``spy_df``); the live
scanner classified before the market context was loaded, so the axis could never
fire while ``behavioral.breadth_divergence_penalty`` shipped active — live sized
larger than the validated model on a narrow rally. These tests pin the call
shape on BOTH paths so the two cannot drift apart again.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import main
from core.behavioral import classify_behavioral_state


def _spy_at_20d_high(n: int = 40) -> pd.DataFrame:
    """SPY closing at its own 20-bar high — half of the divergence condition."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series(range(100, 100 + n), dtype=float, index=idx)
    return pd.DataFrame({"open": close, "high": close, "low": close - 1.0,
                         "close": close, "volume": 1e6}, index=idx)


def _narrow_breadth(n: int = 40) -> pd.DataFrame:
    """pct_above_ma200 well under the 55% divergence threshold."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({"pct_above_ma200": 42.0}, index=idx)


_SETTINGS = {"behavioral": {"breadth_divergence_penalty": 0.2}}


def test_divergence_needs_the_spy_frame():
    """The axis is inert without spy_df — this is what made the leak invisible."""
    data = {"breadth": _narrow_breadth()}
    assert classify_behavioral_state(data, settings=_SETTINGS,
                                     spy_df=None).breadth_divergence is False
    assert classify_behavioral_state(data, settings=_SETTINGS,
                                     spy_df=_spy_at_20d_high()).breadth_divergence is True


def test_live_pipeline_passes_spy_to_the_behavioral_classifier(monkeypatch):
    """Drives _run_pipeline end-to-end: the state reaching the per-ticker loop
    must have been classified from the loaded market context, keyed "SPY" — the
    same key backtest/loader.py uses. Asserting on the helper alone would not
    catch _run_pipeline being unhooked from it."""
    seen = {}
    spy = _spy_at_20d_high()

    def _spy_capturing_classify(data, settings=None, spy_df=None, as_of=None):
        seen["spy_df"] = spy_df
        return classify_behavioral_state(data, settings=settings, spy_df=spy_df,
                                         as_of=as_of)

    import core.behavioral as behavioral
    monkeypatch.setattr(behavioral, "classify_behavioral_state", _spy_capturing_classify)
    monkeypatch.setattr(main, "_load_market_context",
                        lambda tickers, now=None: ({"SPY": spy, "QQQ": spy}, None))
    monkeypatch.setattr(main, "load_open_positions", lambda: {})
    monkeypatch.setattr(main, "_expected_hold_range", lambda engine: (5, 25))
    monkeypatch.setattr(main, "_load_ticker_health", lambda engine: None)
    monkeypatch.setattr(main, "_process_ticker",
                        lambda ticker, engine, **kw: seen.setdefault(
                            "state", kw["behavioral_state"]))

    main._run_pipeline(["ANY"], SimpleNamespace(_today=None), settings=_SETTINGS,
                       behavioral_data={"breadth": _narrow_breadth()},
                       now=datetime(2025, 2, 26, tzinfo=timezone.utc))

    assert seen["spy_df"] is spy
    assert seen["state"].breadth_divergence is True


def test_missing_spy_frame_degrades_without_raising(caplog):
    """No SPY in the context (fetch failure) must warn, not crash — and must say
    the run's sizing diverges from the backtest."""
    with caplog.at_level("WARNING"):
        state = main._classify_behavioral({"breadth": _narrow_breadth()}, _SETTINGS, {})
    assert state.breadth_divergence is False
    assert any("breadth_divergence cannot" in r.message for r in caplog.records)


@pytest.mark.parametrize("path_settings", [_SETTINGS, {}])
def test_backtest_and_live_agree_on_the_same_inputs(path_settings):
    """Same feeds + same SPY frame → identical behavioral verdict on both paths.
    The backtester calls classify_behavioral_state directly with spy_df/as_of;
    live now routes through _classify_behavioral with the same frame."""
    data = {"breadth": _narrow_breadth()}
    spy = _spy_at_20d_high()
    bt = classify_behavioral_state(data, settings=path_settings, spy_df=spy)
    live = main._classify_behavioral(data, path_settings, {"SPY": spy})
    assert bt.breadth_divergence == live.breadth_divergence
    assert bt.size_multiplier == live.size_multiplier
