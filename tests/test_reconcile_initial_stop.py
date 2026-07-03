"""
Reconciliation must use the INITIAL stop as the realized-R denominator (audit D1),
so a later trailed stop_price can't drift the live-vs-backtest meter. Positions
without an initial_stop (legacy, pre-migration) fall back to stop_price.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "live"))

import reconcile_fills as rf  # noqa: E402
from core.position_manager import Position  # noqa: E402


def _closed(side, entry, stop, exit_price, *, initial_stop, ticker="TEST.1"):
    return Position(
        id=1, ticker=ticker, side=side, entry_price=entry,
        entry_date=date(2026, 1, 1), stop_price=stop, initial_stop=initial_stop,
        exit_price=exit_price, exit_date=date(2026, 2, 1),
    )


def test_reconcile_scores_against_initial_not_trailed_stop():
    # long entry 100, INITIAL stop 90 (risk 10), stop later trailed up to 98,
    # exit 110. R against the initial stop = (110-100)/(100-90) = 1.0;
    # against the trailed stop it would be a wrong 5.0.
    p = _closed("long", 100.0, 98.0, 110.0, initial_stop=90.0)
    out = rf.reconcile([p], commission_r=0.0)
    assert out["scored"][0][1] == pytest.approx(1.0)


def test_reconcile_falls_back_to_stop_when_initial_absent():
    # Legacy row: initial_stop is None -> use stop_price 90 -> R = 1.0.
    p = _closed("long", 100.0, 90.0, 110.0, initial_stop=None)
    out = rf.reconcile([p], commission_r=0.0)
    assert out["scored"][0][1] == pytest.approx(1.0)


def test_open_position_initial_stop_in_dataclass():
    # The Position dataclass carries initial_stop (defaults to None).
    p = Position(id=1, ticker="TEST.1", side="long", entry_price=100.0,
                 entry_date=date(2026, 1, 1), stop_price=95.0, initial_stop=95.0)
    assert p.initial_stop == 95.0
