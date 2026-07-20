"""Bar-ladder fidelity for backtest/counterfactual.py.

No FilterEngine is ever constructed: the engine layer is reached through the
``ExitProbe`` seam, so the exit chain is scripted with a plain callable and every
expected outcome is exact arithmetic.

Geometry used throughout: close 100, atr 4.0, atr_mult 2.5 → stop distance 10 →
long stop 90, target 125 (min_rr 2.5). One unit of risk is therefore 10 points
off a 100 entry, so R values are read straight off the price.

The final section is the contract test: the same synthetic frame driven through
``BarReplayBacktester._walk`` and through ``replay_counterfactual`` must agree on
R, reason, exit bar and MFE/MAE. That is what proves this module replicates the
ladder rather than merely implementing one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.counterfactual import replay_counterfactual

ATR = 4.0
STOP = 90.0          # 100 - 4.0*2.5
TARGET = 125.0       # 100 + 10*2.5


def _frame(n: int = 40, close: float = 100.0, atr: float = ATR) -> pd.DataFrame:
    """Flat OHLC with a constant ATR column. Bars are perfectly flat, so any
    single overridden high/low triggers exactly one rung of the ladder."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": np.full(n, close), "high": np.full(n, close),
         "low": np.full(n, close), "close": np.full(n, close),
         "atr": np.full(n, atr)},
        index=idx,
    )


def _set(df: pd.DataFrame, k: int, **cols) -> pd.DataFrame:
    for c, v in cols.items():
        df.iloc[k, df.columns.get_loc(c)] = v
    return df


# signal bar 0 → entry bar 1 at open 100
SIG = 0
ENTRY = 1


def _replay(df, **kw):
    kw.setdefault("breakeven_trigger_r", None)   # ratchet off unless a test wants it
    return replay_counterfactual(df, signal_idx=SIG, ticker="TEST.1", **kw)


# ── geometry and entry ──────────────────────────────────────────────────────

def test_geometry_is_built_off_the_signal_bar_close():
    r = _replay(_frame())
    assert r.initial_stop == pytest.approx(STOP)
    assert r.initial_target == pytest.approx(TARGET)
    assert r.entry_idx == ENTRY
    assert r.entry_price == pytest.approx(100.0)


def test_no_fill_bar_returns_none():
    df = _frame(n=6)
    assert replay_counterfactual(df, signal_idx=5, ticker="TEST.1") is None


def test_warmup_atr_returns_none():
    df = _frame()
    df.iloc[SIG, df.columns.get_loc("atr")] = np.nan
    assert _replay(df) is None


def test_explicit_stop_and_target_override_atr_geometry():
    r = _replay(_frame(), stop_price=95.0, target_price=110.0)
    assert (r.initial_stop, r.initial_target) == (95.0, 110.0)


# ── the exit ladder ─────────────────────────────────────────────────────────

def test_stop_fills_at_stop_on_intraday_touch():
    df = _set(_frame(), 5, low=89.0)
    r = _replay(df)
    assert r.exit_reason == "stop"
    assert r.exit_idx == 5
    assert r.exit_price == pytest.approx(STOP)
    assert r.r_multiple == pytest.approx(-1.0)


def test_stop_gaps_through_at_the_open():
    # Opened below the stop → stop-market fills at the open, worse than -1R.
    df = _set(_frame(), 5, open=85.0, low=84.0, high=85.0)
    r = _replay(df)
    assert r.exit_price == pytest.approx(85.0)
    assert r.r_multiple == pytest.approx(-1.5)


def test_target_fills_at_target():
    df = _set(_frame(), 5, high=130.0)
    r = _replay(df)
    assert r.exit_reason == "target"
    assert r.exit_price == pytest.approx(TARGET)
    assert r.r_multiple == pytest.approx(2.5)


def test_stop_beats_target_on_the_same_bar():
    # Pessimistic same-bar convention: both touched, the stop wins.
    df = _set(_frame(), 5, low=89.0, high=130.0)
    r = _replay(df)
    assert r.exit_reason == "stop"
    assert r.r_multiple == pytest.approx(-1.0)


def test_time_stop_closes_at_the_bar_close_when_not_in_profit():
    df = _frame()                       # perfectly flat → never in profit
    r = _replay(df, max_hold_days=5)
    assert r.exit_reason == "time_stop"
    assert r.exit_idx == ENTRY + 5
    assert r.exit_price == pytest.approx(100.0)


def test_time_stop_suppressed_while_in_profit():
    # mode="if_not_profit" holds a winner past the cap. Entry stays at 100 (bar
    # ENTRY untouched); every later bar sits at 110, below the 125 target, so the
    # only rung that could fire is the time stop — and it must not.
    df = _frame()
    for k in range(ENTRY + 1, len(df)):
        _set(df, k, close=110.0, high=110.0, open=110.0, low=110.0)
    r = _replay(df, max_hold_days=5)
    assert r.exit_reason != "time_stop"


def test_open_eod_marks_not_matured():
    df = _frame(n=8)                    # nothing triggers, runs off the end
    r = _replay(df, max_hold_days=None)
    assert r.exit_reason == "open_eod"
    assert r.matured is False


def test_gap_through_entry_books_zero_r():
    # Entry opens below the stop → risk_per_share <= 0; compute_r books 0R.
    df = _set(_frame(), ENTRY, open=85.0, low=85.0, high=85.0, close=85.0)
    r = _replay(df)
    assert r.gapped_through is True
    assert r.r_multiple == pytest.approx(0.0)


# ── engine exit (the ExitProbe seam) ────────────────────────────────────────

def test_engine_exit_fills_at_the_next_bar_open():
    df = _set(_frame(), 6, open=107.0)
    r = _replay(df, exit_probe=lambda k: k == 5)
    assert r.exit_reason == "engine_exit"
    assert r.exit_idx == 6                       # signalled at 5, filled at 6
    assert r.exit_price == pytest.approx(107.0)


def test_engine_exit_fill_bar_is_excluded_from_excursion():
    # The fill bar's H/L must not enter MFE — _walk fills before update_excursion.
    df = _set(_frame(), 6, open=107.0, high=900.0)
    r = _replay(df, exit_probe=lambda k: k == 5)
    assert r.mfe_r == pytest.approx(0.0)         # flat bars only; 900 excluded


def test_engine_exit_does_not_fire_without_a_probe():
    r = _replay(_frame(n=8), max_hold_days=None)
    assert r.exit_reason == "open_eod"


# ── look-ahead boundary (the gatekeeper) ────────────────────────────────────

def test_breakeven_ratchet_is_only_checked_on_the_following_bar():
    # Bar 3 reaches +1R (high 110) AND falls back to 99 on the same bar. The
    # breakeven stop is set at the END of bar 3, so bar 3 must NOT exit on it;
    # bar 4 dipping below entry is the first bar that can. This test goes red if
    # the ratchet (step 7) is ever moved above the stop check (step 3).
    df = _frame()
    _set(df, 3, high=110.0, low=99.0)
    _set(df, 4, low=99.0, close=99.0)
    r = _replay(df, breakeven_trigger_r=1.0)
    assert r.exit_reason == "breakeven_stop"
    assert r.exit_idx == 4                       # not 3


def test_breakeven_exit_leaves_the_r_denominator_on_the_initial_stop():
    df = _frame()
    _set(df, 3, high=110.0)
    _set(df, 4, low=99.0, close=99.0)
    r = _replay(df, breakeven_trigger_r=1.0)
    assert r.initial_stop == pytest.approx(STOP)  # frozen at 90, not moved to 100
    assert r.r_multiple == pytest.approx(0.0)     # exited at entry
    assert r.mfe_r == pytest.approx(1.0)          # 110 vs 100 entry, risk 10


def test_mfe_and_mae_are_measured_in_r():
    df = _frame()
    _set(df, 2, high=115.0)                       # +1.5R favourable
    _set(df, 3, low=95.0)                         # -0.5R adverse
    r = _replay(df, max_hold_days=None)
    assert r.mfe_r == pytest.approx(1.5)
    assert r.mae_r == pytest.approx(-0.5)


# ── friction ────────────────────────────────────────────────────────────────

def test_commission_is_subtracted_from_r():
    df = _set(_frame(), 5, high=130.0)
    r = _replay(df, commission_r=0.005)
    assert r.r_multiple == pytest.approx(2.5 - 0.005)


def test_exit_slippage_applies_to_market_fills_but_not_the_target_limit():
    stopped = _replay(_set(_frame(), 5, low=89.0), exit_slippage_pct=0.002)
    assert stopped.exit_price == pytest.approx(STOP * (1 - 0.002))

    targeted = _replay(_set(_frame(), 5, high=130.0), exit_slippage_pct=0.002)
    assert targeted.exit_price == pytest.approx(TARGET)   # limit fill stays exact


def test_entry_slippage_moves_the_fill_and_reanchors_the_target():
    r = _replay(_frame(), entry_slippage_pct=0.002)
    assert r.entry_price == pytest.approx(100.0 * 1.002)
    # Target re-anchored to the slipped entry so a hit still pays min_rr.
    assert r.initial_target > TARGET


# ── short side ──────────────────────────────────────────────────────────────

def test_short_geometry_mirrors_long():
    r = _replay(_frame(), direction="short")
    assert r.initial_stop == pytest.approx(110.0)     # above entry
    assert r.initial_target == pytest.approx(75.0)    # 100 - 10*2.5


def test_short_target_pays_the_same_r_as_long():
    df = _set(_frame(), 5, low=70.0)
    r = _replay(df, direction="short")
    assert r.exit_reason == "target"
    assert r.r_multiple == pytest.approx(2.5)


def test_short_stop_is_hit_on_a_rally():
    df = _set(_frame(), 5, high=111.0)
    r = _replay(df, direction="short")
    assert r.exit_reason == "stop"
    assert r.r_multiple == pytest.approx(-1.0)


# ── _walk equivalence (the contract) ────────────────────────────────────────

def _walk_equivalent(df, *, script_exit_at=None, max_hold_days=None,
                     breakeven_trigger_r=None):
    """Drive BarReplayBacktester._walk over `df` with a scripted engine, then the
    counterfactual with the equivalent probe, and return both outcomes.

    The stub engine returns an entry on bar SIG and an exit on `script_exit_at`,
    so both paths see an identical decision sequence.
    """
    from backtest.backtester import BarReplayBacktester, BacktestConfig
    from core.filter_engine import SignalResult

    class _StubEngine:
        _today = None
        cfg = type("C", (), {"events": type("E", (), {"earnings_buffer_two_sided": False})()})()

        def signal(self, ticker, df_t, **kw):
            k = len(df_t) - 1
            if kw.get("held_long") or kw.get("held_short"):
                if script_exit_at is not None and k == script_exit_at:
                    return SignalResult(passed=True, direction="exit_long",
                                        signal_type="stub", reason="stub exit")
                return SignalResult(passed=False, reason="hold")
            if k == SIG:
                return SignalResult(passed=True, direction="long", signal_type="stub",
                                    stop_price=STOP, target_price=TARGET,
                                    min_rr=2.5, size_mult=1.0, reason="stub entry")
            return SignalResult(passed=False, reason="no entry")

    cfg = BacktestConfig(max_hold_days=max_hold_days,
                         breakeven_trigger_r=breakeven_trigger_r)
    bt = BarReplayBacktester(engine=_StubEngine(), cfg=cfg)
    trades, _ = bt._walk(df, "TEST.1", None, None, [], None,
                         df.index[0].date(), df.index[-1].date())

    cf = replay_counterfactual(
        df, signal_idx=SIG, ticker="TEST.1", stop_price=STOP, target_price=TARGET,
        max_hold_days=max_hold_days, breakeven_trigger_r=breakeven_trigger_r,
        exit_probe=(None if script_exit_at is None else (lambda k: k == script_exit_at)),
    )
    return (trades[0] if trades else None), cf


def _assert_same(walk_trade, cf):
    assert walk_trade is not None and cf is not None
    assert cf.exit_reason == walk_trade.exit_reason
    assert cf.r_multiple == pytest.approx(walk_trade.r_multiple)
    assert cf.mfe_r == pytest.approx(walk_trade.mfe_r)
    assert cf.mae_r == pytest.approx(walk_trade.mae_r)
    assert cf.exit_price == pytest.approx(walk_trade.exit_price)
    assert cf.bars_held == walk_trade.bars_held


def test_matches_walk_on_a_stop():
    walk, cf = _walk_equivalent(_set(_frame(), 5, low=89.0))
    _assert_same(walk, cf)


def test_matches_walk_on_a_target():
    walk, cf = _walk_equivalent(_set(_frame(), 5, high=130.0))
    _assert_same(walk, cf)


def test_matches_walk_on_a_time_stop():
    walk, cf = _walk_equivalent(_frame(), max_hold_days=5)
    _assert_same(walk, cf)


def test_matches_walk_on_an_engine_exit():
    walk, cf = _walk_equivalent(_set(_frame(), 6, open=107.0), script_exit_at=5)
    _assert_same(walk, cf)


def test_matches_walk_with_the_breakeven_ratchet_engaged():
    df = _frame()
    _set(df, 3, high=110.0, low=99.0)
    _set(df, 4, low=99.0, close=99.0)
    walk, cf = _walk_equivalent(df, breakeven_trigger_r=1.0)
    _assert_same(walk, cf)
