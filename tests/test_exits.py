"""Shared max-hold (time-stop) exit decision — the single rule main.py and the
backtester both use, so the live feed and backtest never diverge on it."""

from __future__ import annotations

from core.exits import max_hold_exit_due


def test_disabled_when_no_cap():
    assert max_hold_exit_due(bars_held=99, current_close=10, entry_price=9,
                             side="long", max_hold_days=None) is False


def test_not_due_before_cap():
    assert max_hold_exit_due(bars_held=24, current_close=10, entry_price=9,
                             side="long", max_hold_days=25, mode="hard") is False


def test_hard_fires_at_cap_regardless_of_profit():
    # in profit (long) — hard still exits
    assert max_hold_exit_due(bars_held=25, current_close=12, entry_price=10,
                             side="long", max_hold_days=25, mode="hard") is True
    # at a loss — hard exits
    assert max_hold_exit_due(bars_held=30, current_close=8, entry_price=10,
                             side="long", max_hold_days=25, mode="hard") is True


def test_if_not_profit_lets_winners_run():
    # long in profit at the cap → not due (let it run)
    assert max_hold_exit_due(bars_held=25, current_close=12, entry_price=10,
                             side="long", max_hold_days=25, mode="if_not_profit") is False
    # long not in profit at the cap → due
    assert max_hold_exit_due(bars_held=25, current_close=9.5, entry_price=10,
                             side="long", max_hold_days=25, mode="if_not_profit") is True
    # exactly flat counts as "not in profit" → due
    assert max_hold_exit_due(bars_held=25, current_close=10, entry_price=10,
                             side="long", max_hold_days=25, mode="if_not_profit") is True


def test_short_profit_direction():
    # short in profit = price below entry → not due in if_not_profit
    assert max_hold_exit_due(bars_held=25, current_close=8, entry_price=10,
                             side="short", max_hold_days=25, mode="if_not_profit") is False
    # short at a loss = price above entry → due
    assert max_hold_exit_due(bars_held=25, current_close=11, entry_price=10,
                             side="short", max_hold_days=25, mode="if_not_profit") is True
