"""
Tests for short signal triggers, evaluator gating, and the
short-side _signal_entry construction.

Tests are isolated from disk where possible (FilterEngine.from_dict
with synthetic configs). The triggers themselves take row/prev/df
inputs we can synthesize.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from core.filter_engine import FilterEngine, MarketRegime


# ─── helpers ─────────────────────────────────────────────────────────────────


def _load_cfg() -> dict:
    """Production filters.yaml as a base."""
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _engine(*, allow_shorts: bool = True) -> FilterEngine:
    cfg = _load_cfg()
    cfg["signals"]["allow_shorts"] = allow_shorts
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 6, 15)
    return eng


def _row(*, close, macd_hist, rsi, atr=1.0, open=None, high=None, low=None):
    """Build a single pandas Series row with the indicator columns the
    triggers consult."""
    return pd.Series({
        "open": open if open is not None else close,
        "high": high if high is not None else close + 1.0,
        "low": low if low is not None else close - 1.0,
        "close": close,
        "volume": 1_000_000,
        "atr": atr,
        "rsi": rsi,
        "macd": 0.0,
        "macd_signal": 0.0,
        "macd_hist": macd_hist,
        "ma_fast": close - 5.0,
        "ma_slow": close - 10.0,
    })


def _df_with_recent_down_cross(rows_back: int = 1) -> pd.DataFrame:
    """A short MACD-hist history with a recent zero-cross DOWN.

    ``rows_back`` controls how many bars ago the cross happened. The
    trigger's ``max_bars_since_cross`` defaults to 3, so 1-3 are within
    window and 4+ are stale.
    """
    n_bars = max(8, rows_back + 5)
    vals = [0.5] * (n_bars - rows_back - 1) + [0.1] + [-0.2] * rows_back
    return pd.DataFrame({"macd_hist": vals})


# ─── _momentum_short_entry ───────────────────────────────────────────────────


def test_momentum_short_entry_fires_on_fresh_downside_break():
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=-0.20, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.05, rsi=48.0)
    df = _df_with_recent_down_cross(rows_back=1)
    assert bool(eng._momentum_short_entry(row, prev, df)) is True


def test_momentum_short_entry_blocks_positive_hist():
    """Hist > 0 → not a short signal."""
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=+0.10, rsi=45.0)
    prev = _row(close=101.0, macd_hist=+0.15, rsi=48.0)
    df = _df_with_recent_down_cross(rows_back=1)
    assert bool(eng._momentum_short_entry(row, prev, df)) is False


def test_momentum_short_entry_blocks_rsi_out_of_band():
    """RSI 60 is above the short_entry rsi_max=50 — block."""
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=-0.20, rsi=60.0)
    prev = _row(close=101.0, macd_hist=-0.05, rsi=58.0)
    df = _df_with_recent_down_cross(rows_back=1)
    assert bool(eng._momentum_short_entry(row, prev, df)) is False


def test_momentum_short_entry_blocks_stale_cross():
    """Zero-cross 10 bars ago > max_bars_since_cross=3 — block."""
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=-0.30, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.20, rsi=48.0)
    df = _df_with_recent_down_cross(rows_back=10)
    assert bool(eng._momentum_short_entry(row, prev, df)) is False


def test_momentum_short_entry_requires_negative_delta():
    """Delta must be <= -threshold (histogram falling). Positive delta → block."""
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=-0.20, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.30, rsi=48.0)  # delta = +0.10
    df = _df_with_recent_down_cross(rows_back=1)
    assert bool(eng._momentum_short_entry(row, prev, df)) is False


# ─── _mean_rev_short_entry ───────────────────────────────────────────────────


def test_mean_rev_short_entry_fires_on_overbought_with_downtick():
    """RSI 75 > short_entry.rsi_min=70 and delta negative → fire."""
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=-0.10, rsi=75.0)
    prev = _row(close=100.5, macd_hist=+0.05, rsi=78.0)  # delta = -0.15
    assert bool(eng._mean_rev_short_entry(row, prev)) is True


def test_mean_rev_short_entry_blocks_rsi_too_low():
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=-0.10, rsi=55.0)
    prev = _row(close=100.5, macd_hist=+0.05, rsi=58.0)
    assert bool(eng._mean_rev_short_entry(row, prev)) is False


def test_mean_rev_short_entry_blocks_positive_delta():
    """Hist rising → not a short trigger."""
    eng = _engine(allow_shorts=True)
    row = _row(close=100.0, macd_hist=+0.20, rsi=75.0)
    prev = _row(close=100.5, macd_hist=+0.05, rsi=78.0)  # delta = +0.15
    assert bool(eng._mean_rev_short_entry(row, prev)) is False


# ─── _evaluate_entry — short branch ──────────────────────────────────────────


def test_evaluate_entry_emits_short_when_flag_on_and_bear_regime():
    eng = _engine(allow_shorts=True)
    regime = MarketRegime(trend="BEAR", volatility="LOW")
    # Force the trigger to return True so we test the regime/gate logic.
    eng._momentum_short_entry = lambda *a, **kw: True
    eng._mean_rev_short_entry = lambda *a, **kw: False

    row = _row(close=100.0, macd_hist=-0.20, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.05, rsi=48.0)
    df = pd.DataFrame([prev, row])

    direction, sigtype, reason = eng._evaluate_entry(
        row, prev, df, regime, "DOWNTREND",
    )
    assert direction == "short"
    assert sigtype == "momentum"


def test_evaluate_entry_blocks_short_when_flag_off():
    """allow_shorts=False → no short emitted even in BEAR regime."""
    eng = _engine(allow_shorts=False)
    regime = MarketRegime(trend="BEAR", volatility="LOW")
    eng._momentum_short_entry = lambda *a, **kw: True

    row = _row(close=100.0, macd_hist=-0.20, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.05, rsi=48.0)
    df = pd.DataFrame([prev, row])

    direction, _, _ = eng._evaluate_entry(row, prev, df, regime, "DOWNTREND")
    assert direction == "none"


def test_evaluate_entry_blocks_short_when_regime_is_bull():
    """allow_shorts=True but regime is BULL → no short."""
    eng = _engine(allow_shorts=True)
    regime = MarketRegime(trend="BULL", volatility="LOW")
    eng._momentum_short_entry = lambda *a, **kw: True

    row = _row(close=100.0, macd_hist=-0.20, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.05, rsi=48.0)
    df = pd.DataFrame([prev, row])

    direction, _, _ = eng._evaluate_entry(row, prev, df, regime, "DOWNTREND")
    assert direction != "short"


def test_evaluate_entry_blocks_short_when_high_volatility():
    """BEAR + HIGH-vol → blocked (allows_shorts is False under HIGH)."""
    eng = _engine(allow_shorts=True)
    regime = MarketRegime(trend="BEAR", volatility="HIGH")
    eng._momentum_short_entry = lambda *a, **kw: True

    row = _row(close=100.0, macd_hist=-0.20, rsi=45.0)
    prev = _row(close=101.0, macd_hist=-0.05, rsi=48.0)
    df = pd.DataFrame([prev, row])

    direction, _, reason = eng._evaluate_entry(row, prev, df, regime, "DOWNTREND")
    assert direction == "none"
    assert "high volatility" in reason.lower()


def test_evaluate_entry_prefers_long_when_both_regimes_could_fire():
    """Defensive: long branch comes first; if BULL regime, never check shorts."""
    eng = _engine(allow_shorts=True)
    regime = MarketRegime(trend="BULL", volatility="LOW")
    eng._momentum_long = lambda *a, **kw: True
    eng._momentum_short_entry = lambda *a, **kw: True

    row = _row(close=100.0, macd_hist=+0.10, rsi=55.0)
    prev = _row(close=99.0, macd_hist=+0.02, rsi=52.0)
    df = pd.DataFrame([prev, row])

    direction, sigtype, _ = eng._evaluate_entry(row, prev, df, regime, "UPTREND")
    assert direction == "long"
    assert sigtype == "momentum"


# ─── _signal_entry — short SignalResult construction ─────────────────────────


def test_signal_entry_short_stop_above_target_below(monkeypatch):
    """For a short, stop_price > close > target_price."""
    eng = _engine(allow_shorts=True)
    eng._momentum_short_entry = lambda *a, **kw: True

    # Construct a 220-bar df so _min_rows_guard passes.
    rows = [_row(close=100.0, macd_hist=-0.10, rsi=45.0)] * 220
    df = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=220, freq="B"))

    # Force the regime path: BEAR + LOW so allows_shorts is True.
    fake_regime = MarketRegime(trend="BEAR", volatility="LOW")
    eng._market_regime = lambda md, vd: fake_regime
    eng._ticker_trend = lambda d: "DOWNTREND"

    result = eng.signal("ABC", df, market_dfs=None, vix_df=None, earnings_date=None)
    assert result.passed is True
    assert result.direction == "short"
    assert result.signal_type == "momentum"

    # Geometry: stop above close, target below close.
    last_close = float(df["close"].iloc[-1])
    assert result.stop_price > last_close, "short stop must be above entry"
    assert result.target_price < last_close, "short target must be below entry"

    # The risk/reward distances must be symmetric to long math:
    # stop_dist = atr * atr_multiplier. Target = entry - stop_dist * min_rr.
    stop_dist = result.stop_price - last_close
    assert abs(stop_dist - 1.0 * 2.5) < 1e-6  # atr=1, atr_multiplier=2.5
    target_dist = last_close - result.target_price
    assert abs(target_dist - stop_dist * 2.5) < 1e-6  # min_rr=2.5


# ─── held_short dispatch in signal() ─────────────────────────────────────────


def _hold_stub(side: str, called: dict):
    """Make a stub that records calls and returns a real failed SignalResult."""
    from core.filter_engine import SignalResult
    def stub(*a, **kw):
        called[side] += 1
        return SignalResult(passed=False, reason=f"{side}-stub-no-op")

    return stub


def test_signal_held_short_dispatches_to_short_exit():
    """held_short=True must call _signal_exit_short instead of _signal_exit."""
    eng = _engine(allow_shorts=True)

    called = {"long": 0, "short": 0}
    eng._signal_exit = _hold_stub("long", called)
    eng._signal_exit_short = _hold_stub("short", called)
    eng._market_regime = lambda md, vd: MarketRegime(trend="BEAR", volatility="LOW")

    rows = [_row(close=100.0, macd_hist=-0.10, rsi=45.0)] * 220
    df = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=220, freq="B"))

    eng.signal("ABC", df, market_dfs=None, vix_df=None,
               earnings_date=None, held_short=True)
    assert called["short"] == 1, "_signal_exit_short should have been called"
    assert called["long"] == 0


def test_signal_held_long_still_dispatches_to_long_exit():
    """Regression: existing held_long=True path unchanged."""
    eng = _engine(allow_shorts=True)
    called = {"long": 0, "short": 0}
    eng._signal_exit = _hold_stub("long", called)
    eng._signal_exit_short = _hold_stub("short", called)
    eng._market_regime = lambda md, vd: MarketRegime(trend="BULL", volatility="LOW")

    rows = [_row(close=100.0, macd_hist=+0.10, rsi=55.0)] * 220
    df = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=220, freq="B"))

    eng.signal("ABC", df, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=True)
    assert called["long"] == 1
    assert called["short"] == 0


# ─── _signal_exit_short — held-short exit triggers ───────────────────────────


def test_signal_exit_short_regime_flip():
    """BEAR → BULL → cover held short with reason 'regime'."""
    eng = _engine(allow_shorts=True)
    rows = [_row(close=100.0, macd_hist=-0.10, rsi=45.0)] * 220
    df = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=220, freq="B"))
    regime = MarketRegime(trend="BULL", volatility="LOW")  # not BEAR

    result = eng._signal_exit_short("ABC", df, regime)
    assert result.passed is True
    assert result.direction == "exit_short"
    assert result.signal_type == "regime"


def test_signal_exit_short_no_trigger_returns_hold():
    """All exits off, regime still BEAR → no exit fires."""
    eng = _engine(allow_shorts=True)
    eng._cfg["signals"]["exits"] = {
        "regime_flip_short": False,
        "short_cover_pop": False,
        "short_cover_oversold": False,
    }
    rows = [_row(close=100.0, macd_hist=-0.10, rsi=45.0)] * 220
    df = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=220, freq="B"))
    regime = MarketRegime(trend="BEAR", volatility="LOW")

    result = eng._signal_exit_short("ABC", df, regime)
    assert result.passed is False
    assert "no exit" in result.reason.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
