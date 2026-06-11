"""
Risk-budget position cap (``PortfolioConfig.max_open_risk``).

The portfolio cap is a *risk budget* in ``size_mult`` units, not a raw count of
open positions: each open position consumes its own ``size_mult`` (a full-size
position = 1.0, a regime/chronic-reduced 0.25x position = 0.25). A new entry is
dropped once it would push aggregate open risk past the budget. So:

- full-size positions reproduce the old "max N concurrent" behaviour exactly
  (budget B == B full-size positions), and
- half-size positions use half a slot, so the same budget holds twice as many.

These drive the real ``run_prepped`` walk with a stub engine that opens one long
per ticker (far stop/target → only end-of-data closes them), all contending for
the budget on the same fill bar.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult


class _MultiLongEngine:
    """Emit one long per ticker while flat, at a fixed ``size_mult``; then hold."""

    def __init__(self, size_mult: float = 1.0) -> None:
        self._today = None
        self._size_mult = size_mult
        self._emitted: set[str] = set()

    def market_regime(self, market_t, vix_t):  # noqa: D401 - stub
        return MarketRegime(trend="BULL", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False,
               regime=None):
        if held_long or held_short:
            return SignalResult(passed=False, reason="hold")
        if ticker not in self._emitted:
            self._emitted.add(ticker)
            close = float(df["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="long", signal_type="momentum",
                stop_price=close - 50.0, target_price=close + 200.0, min_rr=2.5,
                size_mult=self._size_mult, market_regime="BULL_LOW",
                ticker_trend="UPTREND", reason="stub: long entry",
            )
        return SignalResult(passed=False, reason="no signal")


def _flat_multi_prepped(tickers: list[str], n: int = 40, px: float = 100.0) -> dict:
    """Flat OHLCV for several tickers sharing one calendar — close never moves."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
            "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
            "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.05,
            "ma_fast": px - 2, "ma_slow": px - 5,
        },
        index=idx,
    )
    return {t: SimpleNamespace(df=df.copy(), earnings_history=None) for t in tickers}


def _run(size_mult: float, max_open_risk: float, n_tickers: int = 6):
    eng = _MultiLongEngine(size_mult=size_mult)
    cfg = PortfolioConfig(max_open_risk=max_open_risk, close_open_at_eod=True)
    bt = PortfolioBacktester(engine=eng, cfg=cfg)
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    return bt.run_prepped(_flat_multi_prepped(tickers), skipped={})


def test_full_size_budget_matches_old_count_cap():
    # 6 candidates, each 1.0 risk unit, budget 2.0 → exactly 2 open (== old N=2).
    result = _run(size_mult=1.0, max_open_risk=2.0)
    assert len(result.trades) == 2
    assert all(t.size_mult == 1.0 for t in result.trades)
    # Aggregate open risk never exceeded the budget.
    assert sum(t.size_mult for t in result.trades) <= 2.0 + 1e-9


def test_half_size_positions_use_half_a_slot():
    # Same 2.0 budget, but each position is 0.5 → 4 fit (4 * 0.5 == 2.0).
    result = _run(size_mult=0.5, max_open_risk=2.0)
    assert len(result.trades) == 4
    assert sum(t.size_mult for t in result.trades) == pytest.approx(2.0)


def test_max_open_risk_must_be_positive():
    with pytest.raises(ValueError, match="max_open_risk"):
        PortfolioBacktester(engine=_MultiLongEngine(), cfg=PortfolioConfig(max_open_risk=0.0))
