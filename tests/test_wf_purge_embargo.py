"""
Walk-forward window hygiene: entry cutoff, resolution tail, purge, embargo.

``PortfolioConfig.end_date`` documents itself as the *latest entry date*, but the
walk used to filter BARS by it — so a position opened near the edge was
force-closed there at ``open_eod`` instead of exiting on the real ladder. The
force-closed count is roughly the open-position count regardless of window
length, so a 1-year OOS window lost a ~3x larger share of its trades than a
3-year IS window, and that asymmetry landed on the IS-vs-OOS degradation measure
walk-forward exists to produce.

Letting those trades resolve is what CREATES the seam leak (an in-sample trade
whose outcome lands inside the OOS block), which is why the tail and the purge
are one change and are tested together here.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult


class _OneLongEngine:
    """Open one long per ticker while flat, then hold. Stop/target unreachable,
    so a position closes only at a window edge or the end of data."""

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
        if ticker in self._emitted:
            return SignalResult(passed=False, reason="no signal")
        self._emitted.add(ticker)
        close = float(df["close"].iloc[-1])
        return SignalResult(
            passed=True, direction="long", signal_type="momentum",
            stop_price=close - 500.0, target_price=close + 500.0, min_rr=1.0,
            size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
            reason="stub: long entry",
        )


def _prepped(tickers, n=60, px=100.0, start="2025-01-01"):
    idx = pd.date_range(start, periods=n, freq="B")
    df = pd.DataFrame(
        {"open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
         "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
         "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.05,
         "ma_fast": px - 2, "ma_slow": px - 5},
        index=idx,
    )
    return {t: SimpleNamespace(df=df.copy(), earnings_history=None) for t in tickers}


def _run(**cfg_kw):
    prepped = _prepped(["AAA"])
    cfg = PortfolioConfig(max_open_risk=5.0, close_open_at_eod=True, **cfg_kw)
    bt = PortfolioBacktester(engine=_OneLongEngine(), cfg=cfg)
    return bt.run_prepped(prepped, {})


# ── entry cutoff ──────────────────────────────────────────────────────────────

def test_no_entry_fills_after_end_date():
    """end_date is the ENTRY cutoff: with a tail walked, no trade may still be
    opened inside it. This is what makes the tail an exit-only extension rather
    than a window extension."""
    res = _run(end_date=date(2025, 1, 15), resolve_tail_bars=30)
    assert res.trades, "expected the stub to open a position"
    assert all(t.entry_date <= date(2025, 1, 15) for t in res.trades)


def test_tail_does_not_create_extra_trades():
    """The tail must not change how many trades the window produced — only how
    they ended."""
    short = _run(end_date=date(2025, 1, 15), resolve_tail_bars=0)
    tailed = _run(end_date=date(2025, 1, 15), resolve_tail_bars=30)
    assert len(short.trades) == len(tailed.trades) == 1


# ── resolution tail ───────────────────────────────────────────────────────────

def test_tail_moves_the_exit_past_the_cutoff():
    """Legacy truncation closes at the edge; the tail lets the position run on.
    The stub's stop/target are unreachable, so with a tail it survives to the
    tail cap instead of being force-closed at end_date."""
    truncated = _run(end_date=date(2025, 1, 15), resolve_tail_bars=0)
    tailed = _run(end_date=date(2025, 1, 15), resolve_tail_bars=20)

    assert truncated.trades[0].exit_date <= date(2025, 1, 15)
    assert tailed.trades[0].exit_date > date(2025, 1, 15)


def test_tail_truncation_is_counted_not_hidden():
    """A position the tail could not see out is still force-closed — but it is
    reported, so residual truncation stays visible."""
    res = _run(end_date=date(2025, 1, 15), resolve_tail_bars=5)
    assert res.tail_truncated == 1


def test_full_range_run_is_unaffected():
    """end_date=None → no cutoff, no tail, no purge: the headline path and every
    full-range run must be bit-identical to the legacy behaviour."""
    legacy = _run(resolve_tail_bars=0)
    default = _run()
    assert len(legacy.trades) == len(default.trades)
    assert legacy.trades[0].exit_date == default.trades[0].exit_date
    assert legacy.trades[0].r_multiple == default.trades[0].r_multiple
    assert default.purged_trades == 0


def test_full_range_run_reports_no_truncation():
    """At the true end of data a forced close is correct terminal behaviour, not
    an artifact — so an un-windowed run must report 0 truncated, or the counter
    would read alarming on the headline for no reason."""
    res = _run()
    assert res.trades[0].exit_reason == "open_eod"   # it did close at the last bar
    assert res.tail_truncated == 0                   # but that is not truncation


# ── purge ─────────────────────────────────────────────────────────────────────

def test_purge_drops_trades_resolving_inside_oos():
    """The leak the tail creates: an IS trade exiting on/after oos_start is not
    an independent in-sample observation, so it must not reach the statistics."""
    res = _run(end_date=date(2025, 1, 15), resolve_tail_bars=30,
               purge_exit_from=date(2025, 1, 20))
    assert res.trades == []
    assert res.purged_trades == 1


def test_purge_keeps_trades_that_resolve_before_oos():
    """A trade that closed inside the window is untouched — the purge removes
    overlap, not boundary-adjacent trades in general."""
    res = _run(end_date=date(2025, 1, 15), resolve_tail_bars=2,
               purge_exit_from=date(2025, 3, 1))
    assert len(res.trades) == 1
    assert res.purged_trades == 0


def test_purge_is_off_by_default():
    res = _run(end_date=date(2025, 1, 15), resolve_tail_bars=30)
    assert res.purged_trades == 0
    assert len(res.trades) == 1


# ── embargo (window construction) ─────────────────────────────────────────────

def _wf_engine(embargo_bars: int):
    """WalkForwardEngine over a synthetic 8-year daily calendar. Only window
    construction is exercised — no backtest is run."""
    from backtest.walk_forward import WalkForwardEngine

    idx = pd.date_range("2010-01-01", "2018-01-01", freq="B")
    df = pd.DataFrame({"close": 100.0}, index=idx)
    uni = SimpleNamespace(
        prepped={"AAA": SimpleNamespace(df=df, earnings_history=None)},
        date_range=SimpleNamespace(first=idx[0].date(), last=idx[-1].date()),
        summary=lambda: "stub universe",   # SweepEngine logs it on construction
    )
    return WalkForwardEngine(
        universe=uni, base_cfg={}, base_port_cfg={"max_open_risk": 5.0},
        is_years=3, oos_years=1, step_months=6, re_tune=False,
        embargo_bars=embargo_bars,
    )


def test_embargo_zero_keeps_oos_adjacent():
    """Default behaviour: OOS opens on the first session after is_end."""
    wins = _wf_engine(0).windows()
    assert wins
    for w in wins:
        assert w.embargo_bars == 0
        assert w.oos_start > w.is_end


def test_embargo_skips_that_many_sessions():
    """The gap is counted in TRADING bars, not calendar days — a 25-bar embargo
    must skip exactly 25 sessions, whatever weekends and holidays intervene."""
    cal = _wf_engine(0)._trading_days()
    plain = _wf_engine(0).windows()
    embargoed = _wf_engine(25).windows()

    for w0, w25 in zip(plain, embargoed):
        assert w0.is_end == w25.is_end          # the IS block is unchanged
        assert w25.embargo_bars == 25
        i0, i25 = cal.index(w0.oos_start), cal.index(w25.oos_start)
        assert i25 - i0 == 25


def test_embargo_shortens_the_is_window_never_silently():
    """An embargo that runs past the data must drop the window rather than
    quietly emit one with a smaller gap than requested."""
    for w in _wf_engine(25).windows():
        assert w.embargo_bars == 25


# ── purge wiring: which legs get it ───────────────────────────────────────────

def _captured_port_params(engine, monkeypatch):
    """Record the port_params every _run_one call receives, without running a
    backtest — the wiring is what is under test, not the walk."""
    seen = []

    def fake_run_one(*, cfg, port_params, param_name, param_value, param_label,
                     group, is_baseline, mutations=None):
        seen.append(dict(port_params))
        return SimpleNamespace(
            stats=SimpleNamespace(trades_count=0, expectancy_r=0.0, win_rate=0.0),
            trades=[], purged_trades=0, tail_truncated=0,
            is_baseline=is_baseline, mutations=mutations,
            param_name=param_name, param_value=param_value,
        )

    monkeypatch.setattr(engine._engine, "_run_one", fake_run_one)
    return seen


def test_is_leg_purges_and_oos_leg_does_not(monkeypatch):
    """The core invariant. IS must be purged at oos_start; OOS must not be
    purged at all — dropping trades that resolve past oos_end would throw away
    genuine out-of-sample results."""
    eng = _wf_engine(0)
    seen = _captured_port_params(eng, monkeypatch)
    eng.run()

    wins = eng.windows()
    assert len(seen) == 2 * len(wins), "expected one IS and one OOS run per window"
    for i, w in enumerate(wins):
        is_params, oos_params = seen[2 * i], seen[2 * i + 1]
        assert is_params["end_date"] == w.is_end
        assert is_params["purge_exit_from"] == w.oos_start
        assert oos_params["end_date"] == w.oos_end
        assert oos_params["purge_exit_from"] is None


def test_oos_leg_cannot_inherit_purge_from_base_port_cfg(monkeypatch):
    """A caller putting purge_exit_from in base_port_cfg must not silently turn
    the OOS purge on — 'OOS is unpurged' has to be an invariant of the engine,
    not an accident of the caller."""
    eng = _wf_engine(0)
    eng._base_port_cfg = dict(eng._base_port_cfg, purge_exit_from=date(2011, 1, 1))
    seen = _captured_port_params(eng, monkeypatch)
    eng.run()

    for oos_params in seen[1::2]:
        assert oos_params["purge_exit_from"] is None


def test_retune_purges_every_sweep_candidate(monkeypatch):
    """The mechanism the PR turns on: the IS sweep picks the best config by
    in-sample E[R], so EVERY candidate must be scored on purged trades. Leaving
    the purge off here would reproduce the leak inside config selection, which
    is worse than not purging at all — the leak would then be invisible."""
    from backtest import walk_forward as wf_mod

    eng = _wf_engine(0)
    eng._re_tune = True
    captured = []

    class _FakeSweepEngine:
        def __init__(self, universe, base_cfg, base_port_cfg, n_workers):
            self._base_port = dict(base_port_cfg)

        def run_ofat(self, grid, port_grid, progress=None):
            captured.append(dict(self._base_port))
            best = SimpleNamespace(
                stats=SimpleNamespace(trades_count=99, expectancy_r=0.1, win_rate=0.5),
                trades=[], purged_trades=0, tail_truncated=0,
                is_baseline=False, mutations={}, param_name="p", param_value=1,
            )
            return SimpleNamespace(all_points=[best])

    monkeypatch.setattr(wf_mod, "SweepEngine", _FakeSweepEngine)
    _captured_port_params(eng, monkeypatch)
    eng.run()

    wins = eng.windows()
    assert len(captured) == len(wins), "every IS window must run its own sweep"
    for w, port in zip(wins, captured):
        assert port["purge_exit_from"] == w.oos_start
        assert port["end_date"] == w.is_end
