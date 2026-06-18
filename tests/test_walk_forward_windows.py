"""
Walk-forward window stepping must not crash when the universe's first bar date
falls on a 29/30/31 and a step lands on a shorter month (audit V4). The month
advance clamps the day to the target month's length.
"""

from __future__ import annotations

from datetime import date

from backtest.walk_forward import _advance_months


def test_advance_months_clamps_day_overflow():
    assert _advance_months(date(2001, 3, 31), 6) == date(2001, 9, 30)   # Sept = 30 days
    assert _advance_months(date(2020, 1, 31), 1) == date(2020, 2, 29)   # leap Feb
    assert _advance_months(date(2021, 1, 31), 1) == date(2021, 2, 28)   # non-leap Feb
    assert _advance_months(date(2001, 8, 31), 6) == date(2002, 2, 28)   # year rollover + clamp


def test_advance_months_keeps_safe_days_and_rolls_year():
    assert _advance_months(date(2001, 1, 15), 6) == date(2001, 7, 15)
    assert _advance_months(date(2001, 11, 15), 6) == date(2002, 5, 15)
    assert _advance_months(date(2001, 1, 28), 1) == date(2001, 2, 28)
