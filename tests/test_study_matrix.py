"""
Study-matrix harness readout math (scripts/study_matrix.py).

The matrices drive default-change decisions, so the derived statistics —
calendar-underwater %, split-half R/yr, WR(T) ceiling, legs parsing, venue
subsetting — are pinned here on synthetic trades.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from study_matrix import (  # noqa: E402
    half_r_per_year, parse_legs_spec, subset_tickers, underwater_pct, wrt_table,
)


def _trade(entry, exit_, r, mfe=0.0, mult=1.0):
    return SimpleNamespace(entry_date=entry, exit_date=exit_, r_multiple=r,
                           mfe_r=mfe, mae_r=0.0, size_mult=mult)


def test_underwater_pct_counts_calendar_days():
    import pandas as pd
    # equity: +1 (peak), then -2 (under), recovers +3 ten days later
    curve = SimpleNamespace(drawdown=pd.Series(
        [0.0, 2.0, 0.0],
        index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-12"]),
    ))
    # Days 01-02..01-11 are underwater (ffill over the gap) = 10 of 12 days
    assert abs(underwater_pct(curve) - 10 / 12) < 1e-9


def test_half_r_per_year_splits_at_2013():
    trades = [
        _trade(date(2005, 1, 1), date(2006, 1, 1), +2.0),   # half 1: +2R over 1y
        _trade(date(2020, 1, 1), date(2021, 1, 1), +4.0),   # half 2: +4R over 1y
    ]
    h1, h2 = half_r_per_year(trades)
    assert abs(h1 - 2.0) < 0.1
    assert abs(h2 - 4.0) < 0.1


def test_half_r_per_year_weights_by_size_mult():
    trades = [_trade(date(2020, 1, 1), date(2021, 1, 1), +4.0, mult=0.5)]
    _, h2 = half_r_per_year(trades)
    assert abs(h2 - 2.0) < 0.1  # effective R = r × size_mult


def test_wrt_table_probabilities_and_naive_expectancy():
    trades = [
        _trade(date(2020, 1, 1), date(2020, 2, 1), +2.0, mfe=2.5),
        _trade(date(2020, 1, 1), date(2020, 2, 1), -1.0, mfe=0.4),
        _trade(date(2020, 1, 1), date(2020, 2, 1), -1.0, mfe=0.0),
        _trade(date(2020, 1, 1), date(2020, 2, 1), +0.1, mfe=1.0),
    ]
    rows = {t: (p, naive) for t, p, naive in wrt_table(trades, rungs=[0.5, 1.0])}
    # T=0.5: mfe>=0.5 → 2 of 4
    assert rows[0.5][0] == 0.5
    # naive E = 0.5*0.5 + mean(-1,-1)*0.5 = 0.25 - 0.5 = -0.25
    assert abs(rows[0.5][1] - (-0.25)) < 1e-9
    # T=1.0: reachers 2/4; rest mean = (-1 + -1)/2 = -1
    assert rows[1.0][0] == 0.5
    assert abs(rows[1.0][1] - (1.0 * 0.5 - 1.0 * 0.5)) < 1e-9


def test_parse_legs_spec_routes_cfg_vs_port():
    legs = parse_legs_spec(
        "stress:signals.stop_loss.min_rr=1.5,max_hold_days=10,"
        "entry_slippage_pct=0.003;gated:regime.vix_slope_block=true,tickers=to"
    )
    assert legs[0]["label"] == "stress"
    assert legs[0]["cfg_mut"] == {"signals.stop_loss.min_rr": 1.5}
    assert legs[0]["port_mut"] == {"max_hold_days": 10,
                                   "entry_slippage_pct": 0.003}
    assert legs[1]["cfg_mut"] == {"regime.vix_slope_block": True}
    assert legs[1]["tickers"] == "to"


def test_subset_tickers_keeps_context():
    prepped = {"AAPL": 1, "CNR.TO": 2, "SPY": 3, "^VIX": 4, "QQQ": 5}
    to = subset_tickers(prepped, "to")
    assert set(to) == {"CNR.TO", "SPY", "^VIX", "QQQ"}
    us = subset_tickers(prepped, "us")
    assert set(us) == {"AAPL", "SPY", "^VIX", "QQQ"}
    assert subset_tickers(prepped, "all") is prepped
