"""
Money-path fill/slice invariants.

Locks the execution behavior the engine relies on so a future edit cannot
silently re-introduce same-bar (T, not T+1) leakage or drop the friction path:

  - an entry signal on bar T fills at the NEXT bar's OPEN (T+1), never the
    signal bar's close;
  - when a single bar's range touches BOTH the stop and the target, the
    pessimistic resolution fills the STOP;
  - bars_held equals the entry -> exit index gap;
  - the portfolio path applies entry slippage and per-trade commission to R;
  - the portfolio walk slices market context to <= the decision bar (causal).

These pass on current code by design — they are the regression guard for the
feed-alignment work that follows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.backtester import BarReplayBacktester, BacktestConfig
from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult


# ── single-name backtester helpers ─────────────────────────────────────────────

def _ohlc_open_ne_close(n: int = 260) -> pd.DataFrame:
    """OHLC where open = close + 10, so a fill price uniquely identifies both
    which bar and which field it came from (open vs close)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = [100.0 + i for i in range(n)]
    open_ = [c + 10.0 for c in close]
    return pd.DataFrame(
        {
            "open": open_,
            "high": [o + 1.0 for o in open_],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": [1_000_000.0] * n,
            "atr": [1.0] * n, "rsi": [55.0] * n,
            "macd": [0.05] * n, "macd_signal": [0.0] * n, "macd_hist": [0.05] * n,
            "ma_fast": [c - 2.0 for c in close], "ma_slow": [c - 5.0 for c in close],
        },
        index=idx,
    )


class _OneShotLong:
    """Emit exactly one long on the first flat bar; then hold forever.

    stop_off / target_off are subtracted/added from the signal-bar close to
    place the initial stop and target.
    """

    def __init__(self, stop_off: float = 50.0, target_off: float = 200.0) -> None:
        self._today = None
        # min-rows guard read by the backtester (self._engine.cfg.trend.ma_slow)
        self.cfg = SimpleNamespace(trend=SimpleNamespace(ma_slow=200))
        self._stop_off = stop_off
        self._target_off = target_off
        self.fire_ts = None

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False, regime=None):
        if held_long or held_short or self.fire_ts is not None:
            return SignalResult(passed=False, reason="hold")
        self.fire_ts = df.index[-1]
        close = float(df["close"].iloc[-1])
        return SignalResult(
            passed=True, direction="long", signal_type="momentum",
            stop_price=close - self._stop_off, target_price=close + self._target_off,
            min_rr=2.5, size_mult=1.0, market_regime="BULL_LOW",
            ticker_trend="UPTREND", reason="stub entry",
        )


def _single_bt(engine, df, monkeypatch, start_pos: int = 210) -> BarReplayBacktester:
    from backtest import backtester as bt_mod
    monkeypatch.setattr(bt_mod, "_load_ohlcv_fallback", lambda ticker: df)
    monkeypatch.setattr(bt_mod, "_attach_indicators", lambda d: d)
    engine._today = df.index[0].date()
    cfg = BacktestConfig(
        start_date=df.index[start_pos].date(),
        end_date=df.index[-1].date(),
        earnings_aware=False,
    )
    return BarReplayBacktester(engine=engine, cfg=cfg)


def test_entry_fills_at_next_bar_open_not_signal_close(monkeypatch):
    df = _ohlc_open_ne_close()
    eng = _OneShotLong()
    res = _single_bt(eng, df, monkeypatch).run("SYNTH")

    assert res.skipped_reason == ""
    assert len(res.trades) == 1
    t = res.trades[0]

    fire_pos = df.index.get_loc(eng.fire_ts)
    entry_ts = df.index[fire_pos + 1]
    # Filled at the T+1 OPEN — never the signal bar's close, never the T+1 close.
    assert t.entry_date == entry_ts.date()
    assert t.entry_price == pytest.approx(float(df.loc[entry_ts, "open"]))
    assert t.entry_price != pytest.approx(float(df.iloc[fire_pos]["close"]))
    assert t.entry_price != pytest.approx(float(df.loc[entry_ts, "close"]))


def test_same_bar_stop_and_target_resolves_to_stop(monkeypatch):
    n = 260
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    shock_pos = 216  # a few bars after the T+1 fill (entry fires at 210 -> fills 211)
    o = [100.0] * n
    h = [100.5] * n
    low = [99.5] * n
    c = [100.0] * n
    # A single wide-range bar that spans BOTH the stop (90) and the target (110).
    low[shock_pos] = 80.0
    h[shock_pos] = 120.0
    df = pd.DataFrame(
        {
            "open": o, "high": h, "low": low, "close": c,
            "volume": [1_000_000.0] * n, "atr": [1.0] * n, "rsi": [55.0] * n,
            "macd": [0.05] * n, "macd_signal": [0.0] * n, "macd_hist": [0.05] * n,
            "ma_fast": [98.0] * n, "ma_slow": [95.0] * n,
        },
        index=idx,
    )
    eng = _OneShotLong(stop_off=10.0, target_off=10.0)  # stop 90, target 110
    res = _single_bt(eng, df, monkeypatch).run("SYNTH")

    assert len(res.trades) == 1
    t = res.trades[0]
    # Sanity: the shock bar genuinely touched both levels.
    assert df.iloc[shock_pos]["low"] <= 90.0 and df.iloc[shock_pos]["high"] >= 110.0
    # Pessimistic resolution: the STOP fills, not the target.
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(90.0)  # min(stop=90, bar_open=100)
    entry_pos = df.index.get_loc(eng.fire_ts) + 1
    assert t.bars_held == shock_pos - entry_pos


# ── portfolio (headline) path helpers ──────────────────────────────────────────

class _OneLongPerTicker:
    """Emit one full-size long per ticker while flat (far stop/target); then hold."""

    def __init__(self) -> None:
        self._today = None
        self._emitted: set[str] = set()

    def market_regime(self, market_t, vix_t):
        return MarketRegime(trend="BULL", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False, regime=None):
        if held_long or held_short or ticker in self._emitted:
            return SignalResult(passed=False, reason="hold")
        self._emitted.add(ticker)
        close = float(df["close"].iloc[-1])
        return SignalResult(
            passed=True, direction="long", signal_type="momentum",
            stop_price=close - 50.0, target_price=close + 200.0, min_rr=2.5,
            size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
            reason="stub entry",
        )


def _flat_prepped(ticker: str = "AAA", n: int = 30, px: float = 100.0) -> dict:
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
    return {ticker: SimpleNamespace(df=df, earnings_history=None)}


def test_portfolio_applies_slippage_and_commission_to_r():
    # Control: no slippage, no commission. Flat price -> exactly 0 R.
    bt0 = PortfolioBacktester(
        engine=_OneLongPerTicker(),
        cfg=PortfolioConfig(max_open_risk=5.0, entry_slippage_pct=0.0, commission_r=0.0),
    )
    t0 = bt0.run_prepped(_flat_prepped(), skipped={}).trades[0]
    assert t0.entry_price == pytest.approx(100.0)
    assert t0.r_multiple == pytest.approx(0.0)

    # With friction: entry slips up, commission drags R down.
    bt1 = PortfolioBacktester(
        engine=_OneLongPerTicker(),
        cfg=PortfolioConfig(max_open_risk=5.0, entry_slippage_pct=0.01, commission_r=0.05),
    )
    t1 = bt1.run_prepped(_flat_prepped(), skipped={}).trades[0]
    assert t1.entry_price == pytest.approx(101.0)  # 100 * (1 + 0.01)
    assert t1.exit_reason == "open_eod"
    expected_r = (100.0 - 101.0) / (101.0 - 50.0) - 0.05
    assert t1.r_multiple == pytest.approx(expected_r)
    assert t1.r_multiple < t0.r_multiple  # the cost path genuinely reduces R


def test_portfolio_market_context_is_causal():
    n = 30
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    tdf = pd.DataFrame(
        {
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
            "volume": 1_000_000.0, "atr": 1.0, "rsi": 50.0,
            "macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0,
            "ma_fast": 98.0, "ma_slow": 95.0,
        },
        index=idx,
    )
    spy = pd.DataFrame({"close": [400.0 + i for i in range(n)]}, index=idx)

    seen: list[tuple] = []

    class _Recorder:
        def __init__(self):
            self._today = None

        def market_regime(self, market_t, vix_t):
            return MarketRegime(trend="BULL", volatility="LOW")

        def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
                   earnings_date=None, held_long=False, held_short=False, regime=None):
            if market_dfs and "SPY" in market_dfs:
                seen.append((df.index[-1], market_dfs["SPY"].index.max()))
            return SignalResult(passed=False, reason="spy")

    bt = PortfolioBacktester(engine=_Recorder(), cfg=PortfolioConfig(max_open_risk=5.0))
    bt.run_prepped(
        {"AAA": SimpleNamespace(df=tdf, earnings_history=None)},
        skipped={}, market_dfs={"SPY": spy}, vix_df=None,
    )

    assert seen, "engine.signal was never called with market context"
    for bar_ts, ctx_max in seen:
        assert ctx_max <= bar_ts, f"market context leaked: tail {ctx_max} > bar {bar_ts}"
