"""
Max-hold exit (``time_stop``) — fix for TODO Note 1 (artificial win rate).

A swing strategy must not hold a position indefinitely. With
``PortfolioConfig.max_hold_days`` set, a still-open trade is force-closed at
the bar's CLOSE once it has been held that many trading bars (exit reason
``time_stop``). Stop/target on the same bar take precedence. When
``max_hold_days`` is None (default), the baseline replays unchanged.

These tests drive the real ``run_prepped`` walk with a stub engine that opens
one long and then holds, so only the time-stop (or end-of-data) can close it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult


class _OneLongEngine:
    """Emit exactly one long while flat; hold (no exit signal) thereafter.

    Stop/target are placed far away by the caller's signal so that only the
    time-stop or the end-of-data force-close can end the trade.
    """

    def __init__(self) -> None:
        self._today = None
        self.long_emitted = 0

    def market_regime(self, market_t, vix_t):  # noqa: D401 - stub
        return MarketRegime(trend="BULL", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False,
               regime=None):
        if held_long or held_short:
            return SignalResult(passed=False, reason="hold")
        if self.long_emitted == 0:
            self.long_emitted += 1
            close = float(df["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="long", signal_type="momentum",
                stop_price=close - 50.0, target_price=close + 200.0, min_rr=2.5,
                size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
                reason="stub: long entry",
            )
        return SignalResult(passed=False, reason="no signal")


def _flat_prepped(n: int = 40, px: float = 100.0) -> dict:
    """Flat OHLCV — close never moves, so the trade is never in profit."""
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
    return {"SYNTH": SimpleNamespace(df=df, earnings_history=None)}


def _rising_prepped(n: int = 40) -> dict:
    """Strictly rising close — a held long is in profit on every bar."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    closes = [100.0 + i for i in range(n)]
    df = pd.DataFrame(
        {
            "open": closes, "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes], "close": closes,
            "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
            "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.05,
            "ma_fast": 98.0, "ma_slow": 95.0,
        },
        index=idx,
    )
    return {"SYNTH": SimpleNamespace(df=df, earnings_history=None)}


def _run(prepped, **cfg_kw):
    eng = _OneLongEngine()
    cfg = PortfolioConfig(max_concurrent=5, close_open_at_eod=True, **cfg_kw)
    bt = PortfolioBacktester(engine=eng, cfg=cfg, scorer=None)
    return bt.run_prepped(prepped, skipped={})


def test_hard_time_stop_closes_at_cap():
    result = _run(_flat_prepped(), max_hold_days=10)
    assert len(result.trades) == 1, result.trades
    t = result.trades[0]
    assert t.exit_reason == "time_stop", t.exit_reason
    # Entry fills on bar index 1; time-stop trips when held >= 10 bars, i.e.
    # at bar index 11 → bars_held == 10 (exit_idx - entry_idx).
    assert t.bars_held == 10, t.bars_held


def test_baseline_unchanged_when_off():
    # No max_hold_days → the flat trade survives to end-of-data.
    result = _run(_flat_prepped())
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "open_eod"
    assert result.trades[0].bars_held > 10


def test_if_not_profit_lets_winner_run():
    # Rising price → in profit every bar → 'if_not_profit' never time-stops.
    result = _run(_rising_prepped(), max_hold_days=10,
                  max_hold_mode="if_not_profit")
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "open_eod"
    assert result.trades[0].bars_held > 10


def test_if_not_profit_still_exits_a_flat_trade():
    # Flat price → never in profit → 'if_not_profit' still cuts at the cap.
    result = _run(_flat_prepped(), max_hold_days=10,
                  max_hold_mode="if_not_profit")
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "time_stop"
    assert result.trades[0].bars_held == 10
