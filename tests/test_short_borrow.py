"""
Stock-borrow cost for shorts.

Unit-level: ``Trade.borrow_drag_r`` / ``effective_r`` arithmetic and the
long/zero-rate/open guards. Wiring-level: ``PortfolioBacktester`` reads
``signals.borrow`` off the engine config and stamps the rate onto each
short Trade so the drag flows into ``effective_r``.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from backtest.trade import Trade
from core.filter_engine import MarketRegime, SignalResult


def _short(rate: float, *, bars: int = 10, r: float = 2.0) -> Trade:
    return Trade(
        ticker="X", signal_type="momentum", direction="short",
        entry_date=date(2025, 1, 1), entry_price=100.0,
        initial_stop=102.5, initial_target=93.75,
        exit_date=date(2025, 1, 15), exit_price=95.0, exit_reason="target",
        bars_held=bars, r_multiple=r, size_mult=1.0, borrow_annual_rate=rate,
    )


# ─── unit: borrow_drag_r / effective_r ────────────────────────────────────────


def test_borrow_drag_zero_when_rate_zero():
    t = _short(0.0)
    assert t.borrow_drag_r() == 0.0
    assert t.effective_r == 2.0  # r_multiple × size_mult, no drag


def test_borrow_drag_positive_for_short_with_rate():
    t = _short(0.0252, bars=10)  # daily fee = 100*(0.0252/252)=0.01
    # drag = 0.01 * 10 bars / risk(2.5) = 0.04 R
    assert abs(t.borrow_drag_r() - 0.04) < 1e-9
    assert abs(t.effective_r - (2.0 - 0.04)) < 1e-9


def test_borrow_drag_scales_with_bars_held():
    assert _short(0.0252, bars=20).borrow_drag_r() > _short(0.0252, bars=5).borrow_drag_r()


def test_borrow_drag_zero_for_long():
    t = _short(0.10)
    t.direction = "long"
    t.initial_stop = 97.5  # long stop below entry → risk 2.5
    assert t.borrow_drag_r() == 0.0
    assert t.effective_r == 2.0


def test_borrow_drag_zero_when_open():
    t = _short(0.10)
    t.exit_date = None
    t.exit_price = None
    assert t.borrow_drag_r() == 0.0


# ─── wiring: PortfolioBacktester stamps the configured rate ────────────────────


class _ShortEngine:
    def __init__(self, rate: float) -> None:
        self._today = None
        self.cfg = SimpleNamespace(signals=SimpleNamespace(
            borrow=SimpleNamespace(annual_rate_default=rate, per_ticker={})))
        self.entered = False
        self.exited = False

    def market_regime(self, m, v):
        return MarketRegime(trend="BEAR", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False, regime=None):
        close = float(df["close"].iloc[-1])
        if held_short:
            if not self.exited:
                self.exited = True
                return SignalResult(passed=True, direction="exit_short",
                                    signal_type="regime", stop_price=0.0,
                                    target_price=0.0, min_rr=0.0, size_mult=1.0,
                                    market_regime="BEAR_LOW", ticker_trend="DOWNTREND",
                                    reason="cover")
            return SignalResult(passed=False, reason="hold")
        if not self.entered:
            self.entered = True
            return SignalResult(passed=True, direction="short",
                                signal_type="momentum", stop_price=close + 2.5,
                                target_price=close - 6.25, min_rr=2.5, size_mult=1.0,
                                market_regime="BEAR_LOW", ticker_trend="DOWNTREND",
                                reason="short entry")
        return SignalResult(passed=False, reason="no signal")


def _flat_prepped(n: int = 12) -> dict:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
         "volume": 1e6, "atr": 1.0, "rsi": 45.0, "macd": 0.0,
         "macd_signal": 0.0, "macd_hist": -0.05, "ma_fast": 98.0, "ma_slow": 95.0},
        index=idx,
    )
    return {"SYNTH": SimpleNamespace(df=df, earnings_history=None)}


def test_portfolio_stamps_borrow_rate_on_short_trades():
    eng = _ShortEngine(rate=0.05)
    bt = PortfolioBacktester(eng, PortfolioConfig(max_open_risk=5.0))
    result = bt.run_prepped(_flat_prepped(), skipped={})

    shorts = [t for t in result.trades if t.direction == "short"]
    assert shorts, "expected a closed short trade"
    t = shorts[0]
    assert t.borrow_annual_rate == 0.05
    assert t.borrow_drag_r() > 0.0
    assert t.effective_r < t.r_multiple * t.size_mult  # drag applied


def test_portfolio_default_no_borrow_when_rate_zero():
    eng = _ShortEngine(rate=0.0)
    bt = PortfolioBacktester(eng, PortfolioConfig(max_open_risk=5.0))
    result = bt.run_prepped(_flat_prepped(), skipped={})
    shorts = [t for t in result.trades if t.direction == "short"]
    assert shorts
    # Pin that the rate was actually READ and stamped (not merely defaulted to 0.0
    # by a Trade that never saw the config) — mirrors the rate=0.05 sibling test.
    assert shorts[0].borrow_annual_rate == 0.0
    assert shorts[0].borrow_drag_r() == 0.0
