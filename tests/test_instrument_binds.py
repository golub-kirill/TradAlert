"""Pure tally helpers for scripts/instrument_binds.py (no I/O).

Covers the momentum-fade eligibility/RSI-band classification and the breadth
key-mismatch verdict. The breadth-frequency walk reuses the production predicate
and is exercised end-to-end by the script, not duplicated here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import instrument_binds as ib  # noqa: E402


def _df(macd_hist, rsi, atr):
    return pd.DataFrame({"macd_hist": macd_hist, "rsi": rsi, "atr": atr})


# ── fade_floor_stats ────────────────────────────────────────────────────────

def test_fade_eligibility_requires_cross_down_and_magnitude():
    # bar1->bar2: +1.0 -> -1.0 is a cross-down of magnitude 2.0; atr=1, mhd=0.18
    # → threshold 0.18, delta -2.0 <= -0.18 → eligible. rsi 50 in [30,65] → fire.
    out = ib.fade_floor_stats(_df([1.0, -1.0], [50, 50], [1.0, 1.0]),
                              rsi_min=30, rsi_max=65, min_hist_delta_atr=0.18)
    assert out == {"eligible": 1, "fire": 1, "floor": 0, "ceiling": 0}


def test_fade_floor_binds_when_rsi_below_min():
    out = ib.fade_floor_stats(_df([1.0, -1.0], [50, 25], [1.0, 1.0]),
                              rsi_min=30, rsi_max=65, min_hist_delta_atr=0.18)
    assert out == {"eligible": 1, "fire": 0, "floor": 1, "ceiling": 0}


def test_fade_ceiling_binds_when_rsi_above_max():
    out = ib.fade_floor_stats(_df([1.0, -1.0], [50, 80], [1.0, 1.0]),
                              rsi_min=30, rsi_max=65, min_hist_delta_atr=0.18)
    assert out == {"eligible": 1, "fire": 0, "floor": 0, "ceiling": 1}


def test_magnitude_gate_rejects_tiny_dip():
    # +0.05 -> -0.05 crosses zero but delta -0.10 > -(0.18*1) → not eligible.
    out = ib.fade_floor_stats(_df([0.05, -0.05], [50, 50], [1.0, 1.0]),
                              rsi_min=30, rsi_max=65, min_hist_delta_atr=0.18)
    assert out["eligible"] == 0


def test_no_cross_is_not_eligible():
    # both positive → no zero-cross-down.
    out = ib.fade_floor_stats(_df([2.0, 1.0], [50, 50], [1.0, 1.0]),
                              rsi_min=30, rsi_max=65, min_hist_delta_atr=0.18)
    assert out["eligible"] == 0


def test_merge_fade_stats_sums_buckets():
    a = {"eligible": 2, "fire": 1, "floor": 1, "ceiling": 0}
    b = {"eligible": 3, "fire": 2, "floor": 0, "ceiling": 1}
    assert ib.merge_fade_stats(a, b) == {"eligible": 5, "fire": 3, "floor": 1, "ceiling": 1}


# ── breadth_key_status ──────────────────────────────────────────────────────

def test_breadth_key_status_flags_loader_stem_mismatch():
    # loader stems present, classifier keys absent → mismatch.
    st = ib.breadth_key_status(["sp500_breadth", "sector_ratios", "aaii"])
    assert st["stem_breadth_present"] and not st["has_breadth_key"]


def test_breadth_key_status_ok_when_classifier_keys_present():
    st = ib.breadth_key_status(["breadth", "sector_rotation", "aaii"])
    assert st["has_breadth_key"] and st["has_sector_rotation_key"]
