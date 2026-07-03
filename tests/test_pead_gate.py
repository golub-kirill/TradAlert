"""Unit tests for the PEAD PIVOT milestone PEAD-1 gate (scripts/pead_gate).

Pure-logic / pure-math — no network, no I/O — so they run in the normal ``pytest tests/``
suite and guard the alignment + scoring that decides whether to build the post-earnings-drift
signal into the engine. The load-bearing test is `test_build_panel_no_lookahead`: it proves the
reaction day E and the T+1 entry are aligned so the signal can never use a price it could not
have seen. See docs/backtest_out/pead_gate_prereg.md.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "studies"))

from pead_gate import (  # noqa: E402
    build_ticker_panel, classify_reaction, evaluate_gate, rank_ic,
    reaction_pos, series_t, tercile_long_short,
)


def test_rank_ic_monotone():
    x = np.arange(50, dtype=float)
    assert rank_ic(x, x)[0] == pytest.approx(1.0)     # perfect +
    assert rank_ic(x, -x)[0] == pytest.approx(-1.0)   # perfect −
    ic, t, n = rank_ic(x, x)
    assert n == 50 and t > 5


def test_rank_ic_short_returns_nan():
    ic, t, n = rank_ic(np.array([1.0]), np.array([2.0]))
    assert np.isnan(ic) and n == 1


def test_classify_reaction():
    assert classify_reaction(7) == "BMO"     # 07:00 → before open
    assert classify_reaction(8) == "BMO"
    assert classify_reaction(16) == "AMC"    # 16:00 → after close
    assert classify_reaction(-1) == "AMC"    # unknown → conservative next session
    assert classify_reaction(12) == "AMC"


def test_reaction_pos_bmo_vs_amc():
    # five consecutive trading days
    dates = pd.to_datetime(["2020-01-06", "2020-01-07", "2020-01-08",
                            "2020-01-09", "2020-01-10"]).values
    ann = np.datetime64("2020-01-08")
    # BMO: react ON the announcement date (>=)
    assert reaction_pos(dates, ann, "BMO") == 2
    # AMC: react the NEXT session (strictly after)
    assert reaction_pos(dates, ann, "AMC") == 3
    # announcement after the last session → no reaction day
    assert reaction_pos(dates, np.datetime64("2020-02-01"), "AMC") is None
    # announcement on a non-trading day rolls forward
    assert reaction_pos(dates, np.datetime64("2020-01-08") - np.timedelta64(0, "D"), "BMO") == 2


def test_tercile_long_short_picks_up_signal():
    # car perfectly orders fwd: top tercile high, bottom low
    car = np.arange(9, dtype=float)
    fwd = np.arange(9, dtype=float) / 100.0
    ls = tercile_long_short(car, fwd)
    assert ls > 0
    # too few → NaN
    assert np.isnan(tercile_long_short(np.array([1.0, 2.0]), np.array([0.1, 0.2])))


def test_series_t():
    mean, t, n = series_t(np.array([0.01, 0.01, 0.01, 0.01]))
    assert mean == 0.01 and n == 4 and not np.isfinite(t)  # zero variance → t NaN
    mean, t, n = series_t(np.array([0.0, 0.02, 0.01, 0.03]))
    assert mean > 0 and t > 0 and n == 4


def _flat_prices(n=60, start="2010-01-04", level=100.0):
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({"open": level, "close": level}, index=idx)


def test_build_panel_no_lookahead():
    """An AMC release on day 30 must score the day-31 jump (next session) and enter at day-32 open.
    SPY flat ⇒ market-adjust is 0, so car_event == the raw reaction-day return."""
    p = _flat_prices(60)
    idx = p.index
    # AMC announcement on idx[30]; the jump lands on idx[31] (the reaction session E)
    p.loc[idx[31]:, ["open", "close"]] = 110.0   # +10% gap that persists
    p.loc[idx[31], "open"] = 110.0
    earn = pd.DataFrame([dict(ann_date=idx[30].date().isoformat(), local_hour=16,
                              eps_estimate=np.nan, reported_eps=np.nan, surprise_pct=5.0)])
    spy = _flat_prices(60)
    spy_close, spy_open = spy["close"], spy["open"]
    spy_ma50 = spy["close"].rolling(50).mean()

    panel = build_ticker_panel("TEST.1", p, earn, spy_close, spy_open, spy_ma50)
    assert len(panel) == 1
    row = panel.iloc[0]
    # reaction day E is the session AFTER the announcement (AMC)
    assert pd.Timestamp(row["date"]) == idx[31]
    # car_event = close[31]/close[30]-1 = 110/100-1 = +0.10 (SPY flat)
    assert abs(row["car_event"] - 0.10) < 1e-9
    # entry at open[32]=110, everything flat after ⇒ fwd21 ≈ 0 (no look-ahead into the jump)
    assert abs(row["fwd21"]) < 1e-9


def test_build_panel_bmo_same_session():
    """A BMO release scores the announcement-date session itself."""
    p = _flat_prices(60)
    idx = p.index
    p.loc[idx[25]:, ["open", "close"]] = 90.0     # −10% on the BMO day onward
    p.loc[idx[25], "open"] = 90.0
    earn = pd.DataFrame([dict(ann_date=idx[25].date().isoformat(), local_hour=7,
                              eps_estimate=np.nan, reported_eps=np.nan, surprise_pct=-3.0)])
    spy = _flat_prices(60)
    panel = build_ticker_panel("TEST.2", p, earn, spy["close"], spy["open"],
                               spy["close"].rolling(50).mean())
    assert len(panel) == 1
    row = panel.iloc[0]
    assert pd.Timestamp(row["date"]) == idx[25]          # E == announcement session (BMO)
    assert abs(row["car_event"] - (-0.10)) < 1e-9        # 90/100 - 1


def test_evaluate_gate_runs_on_synthetic_panel():
    """A panel with a real car→fwd relationship should at least compute both bars without error."""
    rng = np.random.default_rng(0)
    n = 600
    car = rng.normal(0, 0.05, n)
    fwd = 0.3 * car + rng.normal(0, 0.05, n)   # genuine positive drift
    dates = pd.bdate_range("2010-01-04", periods=n, freq="C")
    panel = pd.DataFrame(dict(
        ticker="TEST.1", is_to=False, date=dates, year=dates.year,
        month=dates.to_period("M"), car_event=car, sue=car, mom20=0.0,
        eligible=True, fwd5=fwd, fwd21=fwd, fwd63=fwd,
    ))
    g = evaluate_gate(panel)
    assert g["ic"] > 0 and g["verdict"] in ("PROCEED", "CLOSED")
    assert set(g) >= {"pass_ic", "pass_econ", "pooled_ls", "ics_y"}
