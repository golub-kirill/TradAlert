"""
Backtester layering invariants (TODO: Backtester fills & entry geometry).

1. No look-ahead: every ``engine.signal`` call during a replay receives a
   frame sliced to exactly the current bar — never a row dated after
   ``engine._today``. AGENTS.md flagged this as a trusted-but-untested
   invariant; this locks it down with a spy over the real walk.

2. Open-EOD close: a position still open at the last in-window bar is
   force-closed with ``exit_reason == "open_eod"`` on that bar — verifying
   the open_eod path is a real end-of-data close, not a window bug.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from backtest.backtester import BarReplayBacktester, BacktestConfig
from core.filter_engine import FilterEngine, SignalResult


def _synth_df(n: int = 260) -> pd.DataFrame:
    closes = [100.0 + i * 0.10 for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
            "atr": [1.0] * n,
            "rsi": [55.0] * n,
            "macd": [0.05] * n,
            "macd_signal": [0.0] * n,
            "macd_hist": [0.05] * n,
            "ma_fast": [c - 2.0 for c in closes],
            "ma_slow": [c - 5.0 for c in closes],
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def _engine() -> FilterEngine:
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "filters.yaml")
        .read_text(encoding="utf-8")
    )
    eng = FilterEngine.from_dict(cfg)
    eng._today = date(2024, 1, 1)
    return eng


def _patch_loader(df: pd.DataFrame) -> None:
    from backtest import backtester as bt_mod
    bt_mod._load_ohlcv_fallback = lambda ticker: df
    bt_mod._attach_indicators = lambda d: d


# ─── 1. no look-ahead ─────────────────────────────────────────────────────────


def test_engine_never_sees_a_future_bar():
    eng = _engine()
    df = _synth_df()
    calls: list[tuple] = []

    def spy(ticker, df_t, *, market_dfs=None, vix_df=None, earnings_date=None,
            held_long=False, held_short=False, regime=None):
        # The frame's last bar must be exactly "today" — never the future.
        last = df_t.index[-1].date()
        calls.append((eng._today, last, len(df_t)))
        assert last == eng._today, f"future leak: tail={last} today={eng._today}"
        assert df_t.index[-1] == df_t.index.max(), "frame not monotonic/truncated"
        return SignalResult(passed=False, reason="spy")

    eng.signal = spy
    _patch_loader(df)

    bt = BarReplayBacktester(engine=eng, cfg=BacktestConfig(
        start_date=df.index[210].date(), end_date=df.index[-1].date(),
        earnings_aware=False,
    ))
    result = bt.run("SYNTH", market_dfs=None, vix_df=None)

    assert result.skipped_reason == "", result.skipped_reason
    assert len(calls) > 0, "spy was never called"
    # Slices grow by one bar each step (strictly increasing length).
    lengths = [n for _, _, n in calls]
    assert lengths == sorted(lengths) and len(set(lengths)) == len(lengths), \
        "frame length should strictly increase bar-by-bar"


# ─── 2. open-EOD force close ──────────────────────────────────────────────────


def test_open_position_force_closes_open_eod_on_last_bar():
    eng = _engine()
    df = _synth_df()
    state = {"entered": False}

    def fake_signal(ticker, df_t, *, market_dfs=None, vix_df=None,
                    earnings_date=None, held_long=False, held_short=False, regime=None):
        if held_long:
            return SignalResult(passed=False, reason="hold")  # never exit
        if not state["entered"]:
            state["entered"] = True
            close = float(df_t["close"].iloc[-1])
            return SignalResult(
                passed=True, direction="long", signal_type="momentum",
                stop_price=close - 50.0, target_price=close + 200.0, min_rr=2.5,
                size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
                reason="stub entry",
            )
        return SignalResult(passed=False, reason="no signal")

    eng.signal = fake_signal
    _patch_loader(df)

    end_date = df.index[-1].date()
    bt = BarReplayBacktester(engine=eng, cfg=BacktestConfig(
        start_date=df.index[210].date(), end_date=end_date, earnings_aware=False,
    ))
    result = bt.run("SYNTH", market_dfs=None, vix_df=None)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.exit_reason == "open_eod", f"expected open_eod, got {t.exit_reason}"
    assert t.exit_date == end_date, f"open_eod must close on last bar, got {t.exit_date}"
    assert t.is_closed
