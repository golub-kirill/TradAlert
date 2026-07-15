"""Pure classification / excursion / aggregation for
scripts/live/false_positive_report.py (no DB, no cache).

Covers the initial-stop-R excursion geometry (long + short), the DOA / stalled /
gave_back bucket boundaries, and the per-bucket summary that drives the report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "live"))

import false_positive_report as fpr  # noqa: E402


def _bars(highs, lows):
    idx = pd.date_range("2026-01-01", periods=len(highs), freq="D")
    return pd.DataFrame({"high": highs, "low": lows}, index=idx)


# ── excursions ──────────────────────────────────────────────────────────────

def test_excursions_long():
    # entry 100, stop 90 → risk 10; window high 115, low 95.
    bars = _bars([105, 115, 108], [98, 95, 101])
    mfe, mae, n = fpr.excursions(bars, entry_price=100, initial_stop=90, side="long")
    assert mfe == pytest.approx(1.5)   # (115-100)/10
    assert mae == pytest.approx(-0.5)  # (95-100)/10
    assert n == 3


def test_excursions_short():
    # entry 100, stop 110 → risk 10; window high 104, low 88.
    bars = _bars([102, 104, 99], [95, 88, 92])
    mfe, mae, n = fpr.excursions(bars, entry_price=100, initial_stop=110, side="short")
    assert mfe == pytest.approx(1.2)   # (100-88)/10
    assert mae == pytest.approx(-0.4)  # (100-104)/10
    assert n == 3


def test_excursions_degenerate_stop_is_unscored():
    # stop on the wrong side of entry for a long → risk <= 0 → (None, None, n).
    bars = _bars([105], [99])
    mfe, mae, n = fpr.excursions(bars, entry_price=100, initial_stop=105, side="long")
    assert mfe is None and mae is None and n == 1


def test_excursions_empty_window():
    mfe, mae, n = fpr.excursions(_bars([], []), 100, 90, "long")
    assert mfe is None and mae is None and n == 0


# ── classify ────────────────────────────────────────────────────────────────

def test_classify_winner_regardless_of_path():
    assert fpr.classify(0.1, r=0.5) == "winner"   # tiny MFE but closed green
    assert fpr.classify(3.0, r=2.0) == "winner"


def test_classify_loss_buckets_and_boundaries():
    assert fpr.classify(0.10, r=-0.8) == "DOA"        # below 0.25
    assert fpr.classify(0.25, r=-0.5) == "stalled"    # 0.25 is NOT DOA (>= boundary)
    assert fpr.classify(0.99, r=-0.3) == "stalled"
    assert fpr.classify(1.00, r=-0.2) == "gave_back"  # 1.0 is give-back (>= boundary)
    assert fpr.classify(2.50, r=-0.1) == "gave_back"


def test_classify_unscored_when_no_mfe():
    assert fpr.classify(None, r=-1.0) == "unscored"


# ── summarize ───────────────────────────────────────────────────────────────

def _row(bucket, r, mfe, mae=-0.5, bars=5):
    return {"bucket": bucket, "r": r, "mfe_r": mfe, "mae_r": mae, "bars": bars}


def test_summarize_counts_shares_and_doa_rate():
    rows = [
        _row("winner", 1.2, 1.5), _row("winner", 0.8, 1.1),
        _row("DOA", -0.9, 0.1), _row("DOA", -1.0, 0.0), _row("DOA", -0.7, 0.2),
        _row("stalled", -0.6, 0.6),
        _row("gave_back", -0.1, 1.4),
    ]  # n=7, losers=5, DOA=3
    s = fpr.summarize(rows)
    assert s["__n__"] == 7
    assert s["winner"]["n"] == 2
    assert s["DOA"]["n"] == 3
    assert s["DOA"]["pct"] == pytest.approx(3 / 7 * 100)
    assert s["__loss_doa_pct__"] == pytest.approx(60.0)  # 3 of 5 losers
    assert s["DOA"]["avg_r"] == pytest.approx((-0.9 - 1.0 - 0.7) / 3)


def test_summarize_empty():
    s = fpr.summarize([])
    assert s["__n__"] == 0
    assert s["__loss_doa_pct__"] == 0.0
