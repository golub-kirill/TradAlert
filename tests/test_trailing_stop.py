"""
ATR trailing stop (exit-logic Phase 2a): pure decision function + a portfolio
scenario proving it fires a `trail_stop` with R computed off the INITIAL stop
(invariant #1), and that OFF (default) replays the baseline exit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.exits import trailing_stop_level
from core.filter_engine import MarketRegime, SignalResult


# ── pure decision function ─────────────────────────────────────────────────────

def test_trailing_off_or_no_atr_returns_prev():
    assert trailing_stop_level(side="long", highest_high=110, lowest_low=95, atr=2.0,
                               trail_atr_mult=None, prev_stop=None, initial_stop=90.0) is None
    assert trailing_stop_level(side="long", highest_high=110, lowest_low=95, atr=0.0,
                               trail_atr_mult=3.0, prev_stop=92.0, initial_stop=90.0) == 92.0


def test_trailing_long_ratchets_up_floored_at_initial():
    # candidate 110-6=104; base initial 90 -> 104
    assert trailing_stop_level(side="long", highest_high=110, lowest_low=95, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=None, initial_stop=90.0) == 104.0
    # never loosens: lower candidate keeps prev
    assert trailing_stop_level(side="long", highest_high=108, lowest_low=95, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=104.0, initial_stop=90.0) == 104.0
    # early (no favorable move): candidate below initial -> stays initial
    assert trailing_stop_level(side="long", highest_high=92, lowest_low=89, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=None, initial_stop=90.0) == 90.0


def test_trailing_short_ratchets_down():
    assert trailing_stop_level(side="short", highest_high=105, lowest_low=88, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=None, initial_stop=110.0) == 94.0
    # candidate 96 > prev 94 -> stays 94 (never loosens up for a short)
    assert trailing_stop_level(side="short", highest_high=105, lowest_low=90, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=94.0, initial_stop=110.0) == 94.0


def test_trailing_activation_gate():
    assert trailing_stop_level(side="long", highest_high=110, lowest_low=95, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=None, initial_stop=90.0,
                               mfe_r=0.5, activate_r=1.0) is None  # not yet activated
    assert trailing_stop_level(side="long", highest_high=110, lowest_low=95, atr=2.0,
                               trail_atr_mult=3.0, prev_stop=None, initial_stop=90.0,
                               mfe_r=1.5, activate_r=1.0) == 104.0


# ── portfolio scenario: rise then pull back ─────────────────────────────────────

class _OneLongFar:
    """Emit one long with a far stop/target so only the trail (or EOD) can close it."""

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
                            stop_price=close - 50.0, target_price=close + 200.0,
                            min_rr=2.5, size_mult=1.0, market_regime="BULL_LOW",
                            ticker_trend="UPTREND", reason="stub")


def _prepped_rise_fall():
    # close rises 100->120 then falls to 110; high/low = close +/- 1; atr = 1.
    close = [100, 102, 106, 110, 114, 118, 120, 119, 116, 113, 110, 110, 110, 110, 110, 110]
    idx = pd.date_range("2025-01-01", periods=len(close), freq="B")
    df = pd.DataFrame({
        "open": close, "high": [c + 1 for c in close], "low": [c - 1 for c in close],
        "close": close, "volume": 1_000_000.0, "atr": 1.0, "rsi": 55.0,
        "macd": 0.05, "macd_signal": 0.0, "macd_hist": 0.05,
        "ma_fast": 98.0, "ma_slow": 95.0,
    }, index=idx)
    return {"AAA": SimpleNamespace(df=df, earnings_history=None)}


def _run(trail_mult):
    bt = PortfolioBacktester(
        engine=_OneLongFar(),
        cfg=PortfolioConfig(max_open_risk=5.0, trail_atr_mult=trail_mult),
        scorer=None,
    )
    return bt.run_prepped(_prepped_rise_fall(), skipped={}).trades


def test_trail_off_is_baseline_open_eod():
    t = _run(trail_mult=None)[0]
    # No trail, far stop/target -> rides to the end -> open_eod.
    assert t.exit_reason == "open_eod"
    assert t.current_stop is None


def test_trail_on_fires_trail_stop_with_r_off_initial_stop():
    t = _run(trail_mult=3.0)[0]
    assert t.exit_reason == "trail_stop"
    # Entry filled at bar-1 open (102); initial stop 50 -> risk 52. The trail
    # ratcheted to 121(peak high)-... and filled on the pullback in profit.
    assert t.entry_price == pytest.approx(102.0)
    assert t.r_multiple > 0                         # a winning trail exit
    # R uses the INITIAL stop denominator (52), NOT the trailed stop.
    assert t.r_multiple == pytest.approx((t.exit_price - 102.0) / 52.0)
    assert t.exit_price > 102.0                     # exited above entry (locked profit)
