"""backtest.stress — era split + leave-one-out worst cases (display-only)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from backtest.stress import drop_best_era, drop_best_year, era_rows


def _t(year: int, r: float, month: int = 6):
    """Minimal trade shape for build_curve: entry/exit dates + effective R."""
    d = date(year, month, 15)
    return SimpleNamespace(entry_date=d, exit_date=d, r_multiple=r,
                           effective_r=r, size_mult=1.0, direction="long")


def test_era_rows_split_and_skip_empty():
    trades = [_t(2005, 1.0), _t(2005, -0.5), _t(2020, 2.0)]
    rows = era_rows(trades)
    assert [r[0] for r in rows] == ["2000-2010", "2018-2026"]  # 2011-2017 empty → skipped
    assert rows[0][1] == 2 and rows[1][1] == 1


def test_drop_best_year_removes_the_top_contributor():
    trades = [_t(2005, 3.0), _t(2006, 0.5), _t(2007, -1.0)]
    label, ec = drop_best_year(trades)
    assert label == "2005"
    assert round(ec.total_r, 2) == round(0.5 - 1.0, 2)


def test_drop_best_era_exposes_a_one_era_carry():
    # All the profit sits in one era → dropping it flips the sign.
    trades = [_t(2005, 5.0), _t(2005, 4.0), _t(2015, -0.5), _t(2020, -0.5)]
    label, ec = drop_best_era(trades)
    assert label == "2000-2010"
    assert ec.total_r < 0


def test_drop_best_none_when_single_group():
    assert drop_best_year([_t(2005, 1.0), _t(2005, 2.0)]) is None
