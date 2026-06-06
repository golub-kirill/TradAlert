"""
Canonical Volume-by-Price profile (``core.indicators.vbp``).

Each bar's volume is distributed across the price bins its ``[low, high]`` range
spans (proportional to overlap), not dumped into the close bin. Volume is
conserved, and a high-volume node above the current price is found by percentile.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.indicators.vbp import compute_vbp, nearest_high_volume_node_above


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows: (low, high, close, volume)."""
    return pd.DataFrame(
        {
            "low": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "volume": [r[3] for r in rows],
        }
    )


def test_volume_is_conserved():
    df = _bars([(10, 20, 15, 100), (12, 18, 15, 50), (30, 40, 35, 200)])
    vbp = compute_vbp(df, lookback=10, n_bins=12)
    assert vbp.sum() == pytest.approx(350.0)  # 100 + 50 + 200, none lost


def test_volume_spread_across_range_not_only_close_bin():
    # Two bars with identical 10..20 ranges but opposite closes (19.9 vs 10.1).
    # Canonical VBP spreads each bar's volume flat across the whole range, so the
    # profile is ~uniform. The old close-bin algorithm would pile each bar's
    # volume into a single (different) bin → only 2 occupied bins.
    df = _bars([(10.0, 20.0, 19.9, 100.0), (10.0, 20.0, 10.1, 100.0)])
    vbp = compute_vbp(df, lookback=10, n_bins=10)
    occupied = vbp[vbp > 0]
    assert len(occupied) >= 5                       # spread across the range
    assert vbp.max() / occupied.min() < 1.5         # ~flat, not close-piled


def test_zero_range_bar_lands_in_one_bin_and_conserves_volume():
    df = _bars([(15.0, 15.0, 15.0, 100.0), (10.0, 20.0, 15.0, 100.0)])
    vbp = compute_vbp(df, lookback=10, n_bins=10)
    assert vbp.sum() == pytest.approx(200.0)


def test_nearest_high_volume_node_above_price():
    # Heavy node at 30..40; price 15 → nearest qualifying node is above it.
    df = _bars([(10, 12, 11, 10), (30, 40, 35, 1000), (14, 16, 15, 10)])
    vbp = compute_vbp(df, lookback=10, n_bins=12)
    node = nearest_high_volume_node_above(vbp, price=15.0, volume_percentile=70)
    assert node is not None
    assert node[0] > 15.0


def test_short_window_returns_empty():
    assert compute_vbp(_bars([(10, 20, 15, 100)]), lookback=10).empty
