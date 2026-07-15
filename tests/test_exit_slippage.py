"""Exit-side slippage lever (``PortfolioConfig.exit_slippage_pct``).

The shipped headline fills every exit at the modeled price exactly; the lever
worsens MARKET-type exit fills (stop / engine_exit / time_stop / open_eod) by a
fraction while target fills — limit orders — stay exact. Default 0.0 must be
byte-identical to a config without the field, so the baseline never moves.

Each test drives the real ``run_prepped`` walk with a stub engine on a synthetic
price path that forces exactly one exit reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult

SLIP = 0.002


class _OneLongEngine:
    """Emit one long on the first flat call; optionally fire an engine exit
    once held ``exit_after_bars`` bars past entry."""

    def __init__(self, stop_off: float, target_off: float,
                 exit_after_bars: int | None = None) -> None:
        self._today = None
        self._stop_off = stop_off
        self._target_off = target_off
        self._exit_after = exit_after_bars
        self._emitted = False
        self._held_calls = 0

    def market_regime(self, market_t, vix_t):
        return MarketRegime(trend="BULL", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False,
               regime=None):
        if held_long:
            self._held_calls += 1
            if self._exit_after is not None and self._held_calls >= self._exit_after:
                return SignalResult(
                    passed=True, direction="exit_long", signal_type="momentum",
                    market_regime="BULL_LOW", ticker_trend="UPTREND",
                    reason="stub: engine exit")
            return SignalResult(passed=False, reason="hold")
        if held_short:
            return SignalResult(passed=False, reason="hold")
        if not self._emitted:
            self._emitted = True
            close = float(df["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="long", signal_type="momentum",
                stop_price=close + self._stop_off, target_price=close + self._target_off,
                min_rr=2.5, size_mult=1.0, market_regime="BULL_LOW",
                ticker_trend="UPTREND", reason="stub: long entry")
        return SignalResult(passed=False, reason="no signal")


def _prepped(bars: list[dict]) -> dict:
    """One ticker's OHLC frame from a list of {open,high,low,close} dicts."""
    idx = pd.date_range("2025-01-01", periods=len(bars), freq="B")
    base = {"volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0, "macd": 0.0,
            "macd_signal": 0.0, "macd_hist": -0.05, "ma_fast": 95.0, "ma_slow": 90.0}
    df = pd.DataFrame([{**base, **b} for b in bars], index=idx)
    return {"TEST.1": SimpleNamespace(df=df, earnings_history=None)}


def _bar(o, h, l, c):  # noqa: E741 - l is the OHLC low
    return {"open": o, "high": h, "low": l, "close": c}


def _run(bars, engine, **pcfg):
    cfg = PortfolioConfig(max_open_risk=5.0, close_open_at_eod=True, **pcfg)
    bt = PortfolioBacktester(engine=engine, cfg=cfg)
    res = bt.run_prepped(_prepped(bars), skipped={})
    assert len(res.trades) == 1
    return res.trades[0]


# Signal fires at bar 0 close (100) → entry fills at bar 1 open. Stop/target are
# offsets from the signal close.

def test_stop_fill_worsened_by_exit_slippage():
    # Bar 2 dips through the 95 stop intraday → stop-market fills 95×(1−slip).
    bars = [_bar(100, 100.5, 99.5, 100), _bar(100, 100.5, 99.5, 100),
            _bar(100, 100.5, 90.0, 92.0)]
    t = _run(bars, _OneLongEngine(stop_off=-5, target_off=+200),
             exit_slippage_pct=SLIP)
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(95.0 * (1 - SLIP))


def test_target_limit_fill_stays_exact():
    # Stop 95 / min_rr 2.5 → run_prepped re-anchors the target to entry+risk×rr
    # = 112.5. Bar 2 rallies through it → limit fill at 112.5, NO slippage.
    bars = [_bar(100, 100.5, 99.5, 100), _bar(100, 100.5, 99.5, 100),
            _bar(100, 113.0, 99.5, 112.0)]
    t = _run(bars, _OneLongEngine(stop_off=-5, target_off=+12.5),
             exit_slippage_pct=SLIP)
    assert t.exit_reason == "target"
    assert t.exit_price == pytest.approx(112.5)


def test_time_stop_fill_worsened():
    flat = _bar(100, 100.5, 99.5, 100)
    t = _run([flat] * 8, _OneLongEngine(stop_off=-50, target_off=+200),
             exit_slippage_pct=SLIP, max_hold_days=3, max_hold_mode="hard")
    assert t.exit_reason == "time_stop"
    assert t.exit_price == pytest.approx(100.0 * (1 - SLIP))


def test_engine_exit_fill_worsened():
    flat = _bar(100, 100.5, 99.5, 100)
    t = _run([flat] * 8,
             _OneLongEngine(stop_off=-50, target_off=+200, exit_after_bars=2),
             exit_slippage_pct=SLIP)
    assert t.exit_reason == "engine_exit"
    assert t.exit_price == pytest.approx(100.0 * (1 - SLIP))


def test_open_eod_fill_worsened():
    flat = _bar(100, 100.5, 99.5, 100)
    t = _run([flat] * 4, _OneLongEngine(stop_off=-50, target_off=+200),
             exit_slippage_pct=SLIP)
    assert t.exit_reason == "open_eod"
    assert t.exit_price == pytest.approx(100.0 * (1 - SLIP))


def test_default_zero_is_byte_identical():
    # exit_slippage_pct=0.0 (and the field left at its default) must reproduce
    # the shipped fills exactly — the baseline never moves.
    bars = [_bar(100, 100.5, 99.5, 100), _bar(100, 100.5, 99.5, 100),
            _bar(100, 100.5, 90.0, 92.0)]
    t_default = _run(bars, _OneLongEngine(stop_off=-5, target_off=+200))
    t_zero = _run(bars, _OneLongEngine(stop_off=-5, target_off=+200),
                  exit_slippage_pct=0.0)
    for f in ("entry_price", "exit_price", "exit_reason", "r_multiple", "bars_held"):
        assert getattr(t_default, f) == getattr(t_zero, f)
    assert t_zero.exit_price == 95.0  # bit-exact stop fill, no float residue


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
