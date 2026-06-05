"""
Phase 10.6 validation — check #6: the portfolio must never hold a long
and a short on the same ticker at once.

``PortfolioBacktester`` keys ``open_trades`` by ticker and only queues a
fresh entry when the ticker is flat (``not held``). So when a long is
already open and the engine emits a *short* on that same ticker, the
short must be silently dropped — no second, opposite position opens.

This test drives the real ``run_prepped`` walk with a stub engine that
deliberately tries to short a ticker it is already long, and asserts the
short never materialises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult


class _StubEngine:
    """Emits a long when flat, then keeps trying to short the held long."""

    def __init__(self) -> None:
        self._today = None
        self.long_emitted = 0
        self.short_attempts_while_long = 0

    def market_regime(self, market_t, vix_t):  # noqa: D401 - stub
        return MarketRegime(trend="BEAR", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False,
               regime=None):
        close = float(df["close"].iloc[-1])
        if held_long:
            # Deliberately try to open a SHORT on a ticker we already hold
            # long. The portfolio guard must ignore this.
            self.short_attempts_while_long += 1
            return SignalResult(
                passed=True, direction="short", signal_type="momentum",
                stop_price=close + 2.5, target_price=close - 6.25, min_rr=2.5,
                size_mult=1.0, market_regime="BEAR_LOW", ticker_trend="DOWNTREND",
                reason="stub: attempt short while long held",
            )
        if held_short:
            return SignalResult(passed=False, reason="hold short")
        # Flat → emit exactly one long, with stop/target far away so it
        # stays open for the rest of the timeline.
        if self.long_emitted == 0:
            self.long_emitted += 1
            return SignalResult(
                passed=True, direction="long", signal_type="momentum",
                stop_price=close - 50.0, target_price=close + 200.0, min_rr=2.5,
                size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
                reason="stub: long entry",
            )
        return SignalResult(passed=False, reason="no signal")


def _flat_prepped(n: int = 30) -> dict:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
            "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
            "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.05,
            "ma_fast": 98.0, "ma_slow": 95.0,
        },
        index=idx,
    )
    return {"SYNTH": SimpleNamespace(df=df, earnings_history=None)}


def test_no_concurrent_long_and_short_on_same_ticker():
    eng = _StubEngine()
    cfg = PortfolioConfig(max_open_risk=5.0, close_open_at_eod=True)
    bt = PortfolioBacktester(engine=eng, cfg=cfg, scorer=None)

    result = bt.run_prepped(_flat_prepped(), skipped={})

    # The stub tried to short the held long on every held bar...
    assert eng.short_attempts_while_long > 0, \
        "engine should have attempted a short while the long was held"

    # ...but no short was ever opened: every recorded trade is long.
    directions = [t.direction for t in result.trades]
    assert directions, "expected at least the one long trade"
    assert all(d == "long" for d in directions), \
        f"a short opened alongside the held long: {directions}"
    assert sum(d == "long" for d in directions) == 1, \
        f"expected exactly one long round-trip, got {directions}"

    # And the opposite-direction signals were not parked as capped entries
    # either — they are dropped at dispatch, never queued.
    capped_dirs = [c.signal.direction for c in result.capped_signals]
    assert "short" not in capped_dirs, \
        f"short should never reach the entry queue while long is held: {capped_dirs}"
