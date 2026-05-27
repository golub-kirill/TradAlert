"""
Tests for `core.ticker_health.TickerHealth`.

Covers the streak math, lookback aging, scale interpretation, the
disabled-tracker no-op contract, and CSV loading. The backtester
integration is exercised separately via a smoke run on the real config.

Run with::

    pytest tests/test_ticker_health.py -v
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from core.ticker_health import (
    DEFAULT_SCALE,
    DEFAULT_LOOKBACK_DAYS,
    TickerHealth,
)


# ─── streak math ─────────────────────────────────────────────────────────────


def test_empty_ledger_returns_full_size():
    h = TickerHealth()
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 0
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 1.0
    assert h.is_blocked("ABC", date(2026, 5, 27)) is False


def test_single_loss_no_penalty():
    """One loss is unlucky, not chronic. Default scale starts at threshold=2."""
    h = TickerHealth()
    h.record_trade("ABC", date(2026, 5, 1), -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 1
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 1.0


def test_two_consecutive_losses_halve():
    h = TickerHealth()
    h.record_trade("ABC", date(2026, 5, 1), -1.0)
    h.record_trade("ABC", date(2026, 5, 10), -0.8)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 2
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 0.5


def test_three_consecutive_losses_quarter():
    h = TickerHealth()
    for d in [date(2026, 5, 1), date(2026, 5, 10), date(2026, 5, 20)]:
        h.record_trade("ABC", d, -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 3
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 0.25


def test_four_consecutive_losses_block():
    h = TickerHealth()
    for d in [date(2026, 5, 1), date(2026, 5, 5), date(2026, 5, 10), date(2026, 5, 20)]:
        h.record_trade("ABC", d, -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 4
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 0.0
    assert h.is_blocked("ABC", date(2026, 5, 27)) is True


def test_win_breaks_streak():
    """A win in the middle of losses resets the streak to zero."""
    h = TickerHealth()
    h.record_trade("ABC", date(2026, 5, 1), -1.0)
    h.record_trade("ABC", date(2026, 5, 5), +2.0)  # win — resets
    h.record_trade("ABC", date(2026, 5, 10), -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 1
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 1.0


def test_scratch_trade_breaks_streak():
    """r == 0 is treated as non-loss; should reset streak (no_penalty)."""
    h = TickerHealth()
    h.record_trade("ABC", date(2026, 5, 1), -1.0)
    h.record_trade("ABC", date(2026, 5, 5), 0.0)
    h.record_trade("ABC", date(2026, 5, 10), -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 1


# ─── lookback aging ──────────────────────────────────────────────────────────


def test_losses_outside_lookback_dont_count():
    h = TickerHealth(lookback_days=30)
    h.record_trade("ABC", date(2026, 1, 1), -1.0)
    h.record_trade("ABC", date(2026, 1, 5), -1.0)
    # Both losses are >30 days before today.
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 0
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 1.0


def test_partial_lookback_window():
    """Old loss ages off, recent two count — streak == 2."""
    h = TickerHealth(lookback_days=30)
    h.record_trade("ABC", date(2026, 1, 1), -1.0)  # too old
    h.record_trade("ABC", date(2026, 5, 5), -1.0)  # in window
    h.record_trade("ABC", date(2026, 5, 15), -1.0)  # in window
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 2


def test_future_trades_skipped():
    """Defensive: trades dated after as_of_date should not be counted."""
    h = TickerHealth()
    h.record_trade("ABC", date(2026, 6, 1), -1.0)  # future
    h.record_trade("ABC", date(2026, 5, 1), -1.0)
    h.record_trade("ABC", date(2026, 5, 10), -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 2


def test_per_ticker_isolation():
    """Penalty on ABC must not affect XYZ."""
    h = TickerHealth()
    for d in [date(2026, 5, 1), date(2026, 5, 10), date(2026, 5, 20)]:
        h.record_trade("ABC", d, -1.0)
    h.record_trade("XYZ", date(2026, 5, 10), -1.0)
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 0.25
    assert h.size_multiplier("XYZ", date(2026, 5, 27)) == 1.0


# ─── enabled flag ────────────────────────────────────────────────────────────


def test_disabled_tracker_always_full_size():
    h = TickerHealth(enabled=False)
    for d in [date(2026, 5, 1), date(2026, 5, 10), date(2026, 5, 20)]:
        h.record_trade("ABC", d, -1.0)
    # Disabled bypasses streak math entirely.
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 1.0
    assert h.is_blocked("ABC", date(2026, 5, 27)) is False
    # But the ledger still records (for debug snapshots later if re-enabled).
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 3


# ─── custom scale ────────────────────────────────────────────────────────────


def test_custom_scale_sparse_keys():
    """Streaks between defined keys use the largest key ≤ streak."""
    h = TickerHealth(scale={2: 0.3, 5: 0.0})
    for d in [date(2026, 5, i) for i in (1, 5, 10, 15)]:  # 4 losses
        h.record_trade("ABC", d, -1.0)
    # streak == 4 → largest key ≤ 4 is 2 → 0.3
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 0.3


def test_empty_scale_never_penalizes():
    h = TickerHealth(scale={})
    for d in [date(2026, 5, i) for i in (1, 5, 10)]:
        h.record_trade("ABC", d, -1.0)
    assert h.size_multiplier("ABC", date(2026, 5, 27)) == 1.0


# ─── config / CSV constructors ───────────────────────────────────────────────


def test_from_config_full_block():
    cfg = {
        "enabled": True,
        "lookback_days": 60,
        "scale": {2: 0.5, 3: 0.25, 4: 0.0},
    }
    h = TickerHealth.from_config(cfg)
    assert h.enabled is True
    assert h.lookback_days == 60
    assert h.scale == {2: 0.5, 3: 0.25, 4: 0.0}


def test_from_config_missing_returns_default_disabled():
    """No config block + fallback_enabled=False → tracker is off."""
    h = TickerHealth.from_config(None)
    assert h.enabled is False
    assert h.lookback_days == DEFAULT_LOOKBACK_DAYS
    assert h.scale == DEFAULT_SCALE


def test_from_config_string_keys_coerced():
    """YAML loaders sometimes give string keys — coerce to int."""
    cfg = {"enabled": True, "scale": {"2": 0.5, "3": 0.25}}
    h = TickerHealth.from_config(cfg)
    assert h.scale == {2: 0.5, 3: 0.25}


def test_from_csv_round_trip(tmp_path: Path):
    p = tmp_path / "trades.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "exit_date", "r_multiple"])
        w.writerow(["ABC", "2026-05-01", "-1.0"])
        w.writerow(["ABC", "2026-05-10", "-0.8"])
        w.writerow(["XYZ", "2026-05-15", "+2.0"])
    h = TickerHealth.from_csv(p)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 2
    assert h.consecutive_losses("XYZ", date(2026, 5, 27)) == 0


def test_from_csv_missing_file_returns_empty():
    """Missing CSV is not an error — log and return empty ledger."""
    h = TickerHealth.from_csv("/tmp/does-not-exist-xyz123.csv")
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 0


def test_from_csv_skips_malformed_rows(tmp_path: Path):
    p = tmp_path / "trades.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "exit_date", "r_multiple"])
        w.writerow(["", "2026-05-01", "-1.0"])  # empty ticker
        w.writerow(["ABC", "not-a-date", "-1.0"])  # bad date
        w.writerow(["ABC", "2026-05-10", "garbage"])  # bad r_multiple
        w.writerow(["ABC", "2026-05-15", "-1.0"])  # valid
    h = TickerHealth.from_csv(p)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 1


# ─── snapshot ────────────────────────────────────────────────────────────────


def test_snapshot_only_includes_active_streaks():
    h = TickerHealth()
    # ABC: in penalty zone
    for d in [date(2026, 5, 1), date(2026, 5, 10)]:
        h.record_trade("ABC", d, -1.0)
    # XYZ: clean — should not appear in snapshot
    h.record_trade("XYZ", date(2026, 5, 10), +2.0)
    snap = h.snapshot(date(2026, 5, 27))
    assert "ABC" in snap
    assert snap["ABC"]["streak"] == 2
    assert snap["ABC"]["multiplier"] == 0.5
    assert "XYZ" not in snap


# ─── boundary case: as_of exactly at lookback edge ───────────────────────────


def test_loss_exactly_on_lookback_boundary_counts():
    """A loss exactly ``lookback_days`` before as_of_date is included."""
    h = TickerHealth(lookback_days=90)
    h.record_trade("ABC", date(2026, 2, 26), -1.0)  # 90 days before 2026-05-27
    h.record_trade("ABC", date(2026, 5, 10), -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 2


def test_loss_one_day_past_lookback_excluded():
    h = TickerHealth(lookback_days=90)
    h.record_trade("ABC", date(2026, 2, 25), -1.0)  # 91 days before
    h.record_trade("ABC", date(2026, 5, 10), -1.0)
    assert h.consecutive_losses("ABC", date(2026, 5, 27)) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
