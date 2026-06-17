"""
Persist the live data-freshness tier on each journaled signal.

`_result_to_row` maps a TickerResult to a scan_results row. These tests pin that a
NEEDS_REVIEW fire carries its tier + reason into the journal (so reconcile_live.py can
exclude it), a clean LIVE fire defaults to tier='LIVE'/review_reason=NULL, and an errored
row with no signal still journals tier='LIVE'.
"""

from __future__ import annotations

from core.types import ScanResult, SignalResult, TickerResult
from persistence.db import _result_to_row


def _fired_long(tier: str = "LIVE", review_reason: str = "") -> TickerResult:
    scan = ScanResult(passed=True, close=100.0, atr=2.0)
    sig = SignalResult(passed=True, direction="long", signal_type="momentum",
                       stop_price=95.0, target_price=110.0,
                       tier=tier, review_reason=review_reason)
    return TickerResult(ticker="TEST.1", scan=scan, signal=sig)


def test_live_entry_persists_live_tier_and_null_reason():
    row = _result_to_row(1, _fired_long())
    assert row["signal_kind"] == "entry_long"
    assert row["tier"] == "LIVE"
    assert row["review_reason"] is None


def test_needs_review_entry_persists_tier_and_reason():
    row = _result_to_row(1, _fired_long(tier="NEEDS_REVIEW",
                                        review_reason="gap 2.3×ATR · stale 1 session"))
    assert row["signal_kind"] == "entry_long"
    assert row["tier"] == "NEEDS_REVIEW"
    assert row["review_reason"] == "gap 2.3×ATR · stale 1 session"


def test_errored_row_without_signal_defaults_to_live_tier():
    scan = ScanResult(passed=False, reason="insufficient data")
    result = TickerResult(ticker="TEST.2", scan=scan, signal=None, error="boom")
    row = _result_to_row(1, result)
    assert row["tier"] == "LIVE"
    assert row["review_reason"] is None
