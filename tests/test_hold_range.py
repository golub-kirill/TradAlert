"""Data-driven 'expected hold' range (single source of truth).

The displayed hold horizon is the 25th-75th percentile of ACTUAL backtest
bars_held (not a hand-set config), computed once per scan and applied to every
fired entry. Display-only — no trade decision reads it.
"""

from __future__ import annotations

import pytest

from backtest import db
from backtest.db import expected_hold_range, hold_range_from_bars


# ── pure: hold_range_from_bars ────────────────────────────────────────────────

def test_too_few_samples_uses_fallback():
    assert hold_range_from_bars([5, 6, 7], fallback=(9, 25)) == (9, 25)


def test_all_equal_collapses_to_point():
    assert hold_range_from_bars([10] * 12, fallback=(9, 25)) == (10, 10)


def test_spread_returns_p25_p75_within_data():
    bars = list(range(5, 45))  # 40 samples, 5..44
    lo, hi = hold_range_from_bars(bars, fallback=(9, 25))
    assert 5 <= lo < hi <= 44          # honest interior range, low < high


def test_ignores_none_zero_and_negative():
    bars = [None, 0, -3] + [12] * 10   # only the ten 12s are valid (>= 8 samples)
    assert hold_range_from_bars(bars, fallback=(9, 25)) == (12, 12)


def test_no_upper_clamp_winners_run_past_cap():
    # if_not_profit lets winners exceed the 25-bar cap; the range must reflect that.
    bars = [30] * 20
    assert hold_range_from_bars(bars, fallback=(10, 25)) == (30, 30)


# ── DB-backed: expected_hold_range (fail-open) ────────────────────────────────

class _FakeCur:
    def __init__(self, bars): self._bars = bars
    def execute(self, sql, params=None): pass
    def fetchall(self): return [{"bars_held": b} for b in self._bars]
    def close(self): pass


class _FakeConn:
    def __init__(self, bars): self._bars = bars
    def cursor(self, dictionary=False): return _FakeCur(self._bars)
    def is_connected(self): return True
    def close(self): pass


def test_expected_hold_range_fallback_is_cap_anchored(monkeypatch):
    def boom(): raise db.MySQLError("no db")
    monkeypatch.setattr(db, "_connect", boom)
    assert expected_hold_range(cap=25) == (10, 25)      # round(0.4*25)=10, cap=25


def test_expected_hold_range_fallback_when_no_run(monkeypatch):
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn([]))
    monkeypatch.setattr(db, "reference_run", lambda cur: None)
    assert expected_hold_range(cap=20) == (8, 20)        # round(0.4*20)=8


def test_expected_hold_range_computes_from_bars(monkeypatch):
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn([14] * 30))
    monkeypatch.setattr(db, "reference_run", lambda cur: {"id": 7})
    assert expected_hold_range(cap=25) == (14, 14)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
