"""
Breakeven stop (exit-logic Phase 2b): pure decision function + a portfolio scenario
proving it moves the stop to entry once the trade reaches the trigger MFE, converting
a give-back LOSS into a ~0R breakeven WITHOUT capping the upside, R off the INITIAL stop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.exits import breakeven_stop_level
from core.filter_engine import MarketRegime, SignalResult


# ── pure decision function ─────────────────────────────────────────────────────

def test_breakeven_off_or_not_triggered():
    assert breakeven_stop_level(side="long", entry_price=100.0, atr=2.0, breakeven_trigger_r=None,
                                breakeven_buffer_atr=0.0, prev_stop=None, initial_stop=90.0,
                                mfe_r=2.0) is None
    assert breakeven_stop_level(side="long", entry_price=100.0, atr=2.0, breakeven_trigger_r=1.0,
                                breakeven_buffer_atr=0.0, prev_stop=None, initial_stop=90.0,
                                mfe_r=0.5) is None  # not yet reached


def test_breakeven_long_moves_to_entry_and_buffer():
    assert breakeven_stop_level(side="long", entry_price=100.0, atr=2.0, breakeven_trigger_r=1.0,
                                breakeven_buffer_atr=0.0, prev_stop=None, initial_stop=90.0,
                                mfe_r=1.5) == 100.0
    assert breakeven_stop_level(side="long", entry_price=100.0, atr=2.0, breakeven_trigger_r=1.0,
                                breakeven_buffer_atr=0.5, prev_stop=None, initial_stop=90.0,
                                mfe_r=1.5) == 101.0  # entry + 0.5*ATR
    # never loosens below an already-higher stop (e.g. a trail above breakeven)
    assert breakeven_stop_level(side="long", entry_price=100.0, atr=2.0, breakeven_trigger_r=1.0,
                                breakeven_buffer_atr=0.0, prev_stop=105.0, initial_stop=90.0,
                                mfe_r=1.5) == 105.0


def test_breakeven_short_moves_below_entry():
    assert breakeven_stop_level(side="short", entry_price=100.0, atr=2.0, breakeven_trigger_r=1.0,
                                breakeven_buffer_atr=0.5, prev_stop=None, initial_stop=110.0,
                                mfe_r=1.5) == 99.0  # entry - 0.5*ATR; min(110, 99)


# ── portfolio scenario: rise to +1R, then fall back through entry ───────────────

class _OneLongTight:
    """Emit one long with a CLOSE stop (risk 5) so +1R is reachable; far target."""

    def __init__(self):
        self._today = None
        self._done = set()

    def market_regime(self, m, v):
        return MarketRegime(trend="BULL", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None, earnings_date=None,
               held_long=False, held_short=False, regime=None):
        if held_long or held_short or ticker in self._done:
            return SignalResult(passed=False, reason="hold")
        self._done.add(ticker)
        close = float(df["close"].iloc[-1])
        return SignalResult(passed=True, direction="long", signal_type="momentum",
                            stop_price=close - 5.0, target_price=close + 200.0, min_rr=2.5,
                            size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
                            reason="stub")


def _prepped():
    # entry ~100, risk 5. Rises to +2R (high 111), then falls back to 97.
    close = [100, 100, 103, 107, 110, 108, 104, 100, 97, 97, 97, 97]
    idx = pd.date_range("2025-01-01", periods=len(close), freq="B")
    df = pd.DataFrame({
        "open": close, "high": [c + 1 for c in close], "low": [c - 1 for c in close],
        "close": close, "volume": 1_000_000.0, "atr": 1.0, "rsi": 55.0,
        "macd": 0.05, "macd_signal": 0.0, "macd_hist": 0.05, "ma_fast": 98.0, "ma_slow": 95.0,
    }, index=idx)
    return {"AAA": SimpleNamespace(df=df, earnings_history=None)}


def _run(breakeven_trigger):
    bt = PortfolioBacktester(
        engine=_OneLongTight(),
        cfg=PortfolioConfig(max_open_risk=5.0, breakeven_trigger_r=breakeven_trigger),
    )
    return bt.run_prepped(_prepped(), skipped={}).trades[0]


def test_breakeven_off_rides_to_loss():
    t = _run(breakeven_trigger=None)
    assert t.exit_reason == "open_eod"
    assert t.r_multiple < 0          # gave back to a loss with no protection


def test_breakeven_on_converts_giveback_to_breakeven():
    t = _run(breakeven_trigger=1.0)
    assert t.exit_reason == "breakeven_stop"
    assert t.entry_price == pytest.approx(100.0)
    # Stop moved to entry once +1R reached; pullback fills at ~entry -> ~0R.
    assert t.r_multiple == pytest.approx(0.0, abs=1e-9)
    assert t.r_multiple > _run(breakeven_trigger=None).r_multiple  # strictly better than the loss
