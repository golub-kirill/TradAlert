"""Market-state size throttle (``PortfolioConfig.size_throttle``).

Default None → byte-identical baseline; a {date: mult} mapping scales entry
size on the fill date only (exits and held positions untouched). Fixture style
mirrors test_portfolio_risk_budget.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult


class _MultiLongEngine:
    """Emit one long per ticker while flat, at size_mult 1.0; then hold."""

    def __init__(self) -> None:
        self._today = None
        self._emitted: set[str] = set()

    def market_regime(self, market_t, vix_t):
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
                size_mult=1.0, market_regime="BULL_LOW",
                ticker_trend="UPTREND", reason="stub: long entry",
            )
        return SignalResult(passed=False, reason="no signal")


def _prepped(tickers, n=40, px=100.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
         "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
         "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.05,
         "ma_fast": px - 2, "ma_slow": px - 5},
        index=idx,
    )
    return {t: SimpleNamespace(df=df.copy(), earnings_history=None) for t in tickers}


def _run(size_throttle=None, n_tickers=3):
    cfg = PortfolioConfig(max_open_risk=10.0, close_open_at_eod=True,
                          size_throttle=size_throttle)
    bt = PortfolioBacktester(engine=_MultiLongEngine(), cfg=cfg)
    return bt.run_prepped(_prepped([f"T{i:02d}" for i in range(n_tickers)]),
                          skipped={})


def test_default_none_leaves_sizes_untouched():
    result = _run(size_throttle=None)
    assert result.trades and all(t.size_mult == 1.0 for t in result.trades)


def test_throttle_scales_entries_on_the_fill_date():
    base = _run(size_throttle=None)
    fill_dates = {t.entry_date for t in base.trades}
    assert len(fill_dates) == 1          # all stubs fill on the same T+1 bar
    fill = next(iter(fill_dates))
    result = _run(size_throttle={fill: 0.5})
    assert result.trades and all(t.size_mult == 0.5 for t in result.trades)


def test_dates_absent_from_the_mapping_default_to_full_size():
    result = _run(size_throttle={date(1999, 1, 1): 0.25})
    assert result.trades and all(t.size_mult == 1.0 for t in result.trades)


def test_zero_mult_caps_the_entry_instead_of_opening_it():
    base = _run(size_throttle=None)
    fill = next(iter({t.entry_date for t in base.trades}))
    result = _run(size_throttle={fill: 0.0})
    assert not result.trades
    assert result.capped_signals            # dropped as capped, not opened at 0
