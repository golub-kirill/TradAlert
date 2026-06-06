"""Pure R-math and aggregation for scripts/reconcile_fills.py (no DB).

The reconciler's DB/print I/O lives in main(); these cover the realized-R
geometry and the bucketing that feed the drift comparison.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reconcile_fills as rf  # noqa: E402
from core.position_manager import Position  # noqa: E402


def _closed(side, entry, stop, exit_price, ticker="AAA"):
    return Position(
        id=1, ticker=ticker, side=side, entry_price=entry,
        entry_date=date(2026, 1, 1), stop_price=stop,
        exit_price=exit_price, exit_date=date(2026, 2, 1),
    )


# ── _risk / _r_multiple ─────────────────────────────────────────────────────

def test_risk_long_and_short():
    assert rf._risk("long", 100, 90) == pytest.approx(10)
    assert rf._risk("short", 100, 110) == pytest.approx(10)


def test_r_multiple_long_win_and_loss():
    assert rf._r_multiple("long", 100, 90, 120) == pytest.approx(2.0)
    assert rf._r_multiple("long", 100, 90, 90) == pytest.approx(-1.0)


def test_r_multiple_short_win():
    assert rf._r_multiple("short", 100, 110, 80) == pytest.approx(2.0)


def test_r_multiple_degenerate_returns_none():
    assert rf._r_multiple("long", 100, 100, 120) is None   # zero risk
    assert rf._r_multiple("long", 100, 110, 120) is None   # stop above entry (long)
    assert rf._r_multiple("short", 100, 90, 80) is None     # stop below entry (short)


# ── reconcile() ─────────────────────────────────────────────────────────────

def test_reconcile_buckets_by_side():
    out = rf.reconcile([
        _closed("long", 100, 90, 120),    # +2.0
        _closed("long", 50, 45, 45),      # -1.0
        _closed("short", 100, 110, 80),   # +2.0
    ], commission_r=0.0)
    assert out["by_side"]["long"] == pytest.approx([2.0, -1.0])
    assert out["by_side"]["short"] == pytest.approx([2.0])
    assert len(out["scored"]) == 3
    assert out["no_stop"] == 0 and out["bad_risk"] == 0


def test_reconcile_counts_unscorable():
    out = rf.reconcile([
        _closed("long", 100, None, 120),  # no stop
        _closed("long", 100, 100, 120),   # zero risk
        _closed("long", 100, 110, 120),   # negative risk
    ], commission_r=0.0)
    assert out["no_stop"] == 1
    assert out["bad_risk"] == 2
    assert out["scored"] == []


def test_reconcile_applies_commission():
    out = rf.reconcile([_closed("long", 100, 90, 120)], commission_r=0.005)
    assert out["by_side"]["long"] == pytest.approx([1.995])
