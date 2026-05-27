"""
Phase 10.3 integration tests — prove the backtester end-to-end produces
short Trade records when the engine emits short signals.

We don't have a real BEAR-regime period in the cached data, so we
synthesize a deterministic OHLCV series and stub the regime classifier
to always return BEAR + LOW. That isolates the backtester wiring
(stop/target/slippage geometry) from the regime classifier.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from backtest.backtester import BarReplayBacktester, BacktestConfig
from core.filter_engine import FilterEngine, SignalResult


def _load_cfg() -> dict:
    p = Path(__file__).resolve().parent.parent / "config" / "filters.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["signals"]["allow_shorts"] = True
    cfg["signals"]["gap_risk"] = {"enabled": False}
    cfg["signals"]["sector_gate"] = {"enabled": False}
    cfg["events"] = {"earnings_buffer_days": 0, "stop_dates": []}
    return cfg


def _synth_df(n_bars: int = 260, trend: str = "down") -> pd.DataFrame:
    """Synthetic OHLCV with steady downtrend or uptrend + warmed indicators."""
    if trend == "down":
        closes = [100.0 - i * 0.10 for i in range(n_bars)]
    else:
        closes = [100.0 + i * 0.10 for i in range(n_bars)]
    df = pd.DataFrame({
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n_bars,
        "atr": [1.0] * n_bars,
        "rsi": [45.0] * n_bars,
        "macd": [-0.10] * n_bars,
        "macd_signal": [-0.05] * n_bars,
        "macd_hist": [-0.05] * n_bars,
        "ma_fast": [c - 2.0 for c in closes],
        "ma_slow": [c - 5.0 for c in closes],
    }, index=pd.date_range("2025-01-01", periods=n_bars, freq="B"))
    return df


def test_backtester_records_short_trade_when_engine_emits_short():
    """End-to-end: stub engine to always emit a short on first bar,
    then immediately emit an exit_short → backtester records one
    closed short trade with direction == 'short'."""
    cfg = _load_cfg()
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 1, 1)

    # Force the engine to emit a single short, then exit_short.
    state = {"emitted_entry": False, "emitted_exit": False}

    def fake_signal(ticker, df, *, market_dfs=None, vix_df=None,
                    earnings_date=None, held_long=False, held_short=False,
                    regime=None):
        if held_short:
            # First held-short call → emit exit_short.
            if not state["emitted_exit"]:
                state["emitted_exit"] = True
                return SignalResult(
                    passed=True, direction="exit_short",
                    signal_type="regime",
                    stop_price=0.0, target_price=0.0, min_rr=0.0,
                    size_mult=1.0, market_regime="BEAR_LOW",
                    ticker_trend="DOWNTREND",
                    reason="test stub: cover short",
                )
            return SignalResult(passed=False, reason="hold short")
        # Flat: emit a single short entry, then nothing.
        if not state["emitted_entry"]:
            state["emitted_entry"] = True
            close = float(df["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="short", signal_type="momentum",
                stop_price=close + 2.5,  # stop above entry
                target_price=close - 6.25,  # target below entry (2.5R)
                min_rr=2.5,
                size_mult=1.0,
                market_regime="BEAR_LOW",
                ticker_trend="DOWNTREND",
                reason="test stub: fresh short entry",
            )
        return SignalResult(passed=False, reason="no signal")

    eng.signal = fake_signal

    df = _synth_df(n_bars=260, trend="down")

    bt_cfg = BacktestConfig(
        start_date=df.index[210].date(),
        end_date=df.index[-1].date(),
        earnings_aware=False,
    )
    bt = BarReplayBacktester(engine=eng, cfg=bt_cfg)

    # Patch the cache load + indicator attach to use our synthetic df.
    bt._store = None  # use fallback path
    from backtest import backtester as bt_mod
    bt_mod._load_ohlcv_fallback = lambda ticker: df
    bt_mod._attach_indicators = lambda d: d

    result = bt.run("SYNTH", market_dfs=None, vix_df=None)
    assert result.skipped_reason == "", f"unexpected skip: {result.skipped_reason}"
    assert len(result.trades) >= 1, "expected at least one short trade"
    shorts = [t for t in result.trades if t.direction == "short"]
    assert len(shorts) >= 1, \
        f"expected ≥1 short, got directions: {[t.direction for t in result.trades]}"

    t = shorts[0]
    assert t.direction == "short"
    # Geometry: stop must be above entry, target below.
    assert t.initial_stop > t.entry_price, "short stop must be above entry"
    assert t.initial_target < t.entry_price, "short target must be below entry"


def test_backtester_short_stop_hit_produces_negative_r():
    """Force a short, then make the very next bar gap UP through the stop.
    Trade must close as 'stop' with r_multiple ≤ -1."""
    cfg = _load_cfg()
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 1, 1)

    state = {"emitted": False}

    def fake_signal(ticker, df, *, market_dfs=None, vix_df=None,
                    earnings_date=None, held_long=False, held_short=False,
                    regime=None):
        if held_short:
            return SignalResult(passed=False, reason="hold short")
        if not state["emitted"]:
            state["emitted"] = True
            close = float(df["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="short", signal_type="momentum",
                stop_price=close + 2.5,
                target_price=close - 6.25,
                min_rr=2.5, size_mult=1.0,
                market_regime="BEAR_LOW", ticker_trend="DOWNTREND",
                reason="test stub: short entry",
            )
        return SignalResult(passed=False, reason="no signal")

    eng.signal = fake_signal

    # Build a df where bar after entry trigger has a high that exceeds stop.
    df = _synth_df(n_bars=260, trend="down").copy()
    # Trigger fires on bar 210 (close=79.0). T+1 entry at bar 211 open=78.9,
    # so stop = 78.9 + 2.5 = 81.4 (set by the stub using the bar-210 close).
    # Make bar 212 gap up through the stop.
    df.iloc[212, df.columns.get_loc("open")] = 84.0
    df.iloc[212, df.columns.get_loc("high")] = 85.0
    df.iloc[212, df.columns.get_loc("low")] = 83.5

    bt = BarReplayBacktester(
        engine=eng,
        cfg=BacktestConfig(
            start_date=df.index[210].date(),
            end_date=df.index[213].date(),
            earnings_aware=False,
        ),
    )
    from backtest import backtester as bt_mod
    bt_mod._load_ohlcv_fallback = lambda ticker: df
    bt_mod._attach_indicators = lambda d: d

    result = bt.run("SYNTH", market_dfs=None, vix_df=None)
    shorts = [t for t in result.trades if t.direction == "short"]
    assert len(shorts) == 1
    t = shorts[0]
    assert t.exit_reason == "stop"
    # Gap-up beyond stop → loss worse than -1R (the apply_stop_fill_short branch).
    assert t.r_multiple < -1.0, f"expected r < -1.0 on gap-up beyond stop, got {t.r_multiple}"


def test_backtester_long_path_unchanged_when_allow_shorts_on():
    """With allow_shorts=True but a stubbed long-only engine, the backtester
    still records long trades correctly (no regression)."""
    cfg = _load_cfg()  # allow_shorts=True
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2025, 1, 1)

    state = {"emitted": False}

    def fake_signal(ticker, df, *, market_dfs=None, vix_df=None,
                    earnings_date=None, held_long=False, held_short=False,
                    regime=None):
        if held_long:
            return SignalResult(passed=True, direction="exit_long",
                                signal_type="regime",
                                stop_price=0.0, target_price=0.0, min_rr=0.0,
                                size_mult=1.0, market_regime="BULL_LOW",
                                ticker_trend="UPTREND",
                                reason="test stub")
        if not state["emitted"]:
            state["emitted"] = True
            close = float(df["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="long", signal_type="momentum",
                stop_price=close - 2.5, target_price=close + 6.25,
                min_rr=2.5, size_mult=1.0,
                market_regime="BULL_LOW", ticker_trend="UPTREND",
                reason="test stub",
            )
        return SignalResult(passed=False, reason="no signal")

    eng.signal = fake_signal
    df = _synth_df(n_bars=260, trend="up")
    bt = BarReplayBacktester(engine=eng, cfg=BacktestConfig(
        start_date=df.index[210].date(),
        end_date=df.index[-1].date(),
        earnings_aware=False,
    ))
    from backtest import backtester as bt_mod
    bt_mod._load_ohlcv_fallback = lambda ticker: df
    bt_mod._attach_indicators = lambda d: d

    result = bt.run("SYNTH", market_dfs=None, vix_df=None)
    longs = [t for t in result.trades if t.direction == "long"]
    assert len(longs) >= 1, "expected ≥1 long trade in the allow_shorts=True path"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
