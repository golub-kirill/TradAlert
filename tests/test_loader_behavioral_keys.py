"""Loader behavioral-key remapping (backtest must match the live data contract).

Parquet files are named by source (sp500_breadth, sector_ratios); the behavioral
classifier reads canonical axis keys (breadth, sector_rotation). Without the remap
those axes silently go missing in backtests. Synthetic data only — no dependence on
the (gitignored) price/behavioral cache.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest import loader
from core.behavioral import classify_behavioral_state


def test_alias_map_covers_the_mismatched_stems():
    assert loader._BEHAVIORAL_KEY_ALIASES["sp500_breadth"] == "breadth"
    assert loader._BEHAVIORAL_KEY_ALIASES["sector_ratios"] == "sector_rotation"


def test_load_behavioral_data_remaps_stems_to_canonical_keys(tmp_path):
    df = pd.DataFrame({"pct_above_ma200": [50.0, 51.0]})
    for stem in ("sp500_breadth", "sector_ratios", "aaii"):
        df.to_parquet(tmp_path / f"{stem}.parquet")

    data = loader._load_behavioral_data(tmp_path)

    assert set(data) == {"breadth", "sector_rotation", "aaii"}
    assert "sp500_breadth" not in data and "sector_ratios" not in data


def test_load_behavioral_data_none_when_absent(tmp_path):
    assert loader._load_behavioral_data(tmp_path / "nope") is None


def test_classifier_consumes_breadth_only_under_canonical_key():
    # 30 strong-breadth bars; SPY flat (no 20d-high divergence).
    idx = pd.date_range("2020-01-01", periods=30, freq="D")
    breadth = pd.DataFrame({"pct_above_ma200": [80.0] * 30}, index=idx)
    spy = pd.DataFrame(
        {"open": 95.0, "high": 100.0, "low": 90.0, "close": 95.0}, index=idx)

    under_stem = classify_behavioral_state(
        {"sp500_breadth": breadth}, settings={}, spy_df=spy, as_of=None)
    under_key = classify_behavioral_state(
        {"breadth": breadth}, settings={}, spy_df=spy, as_of=None)

    # Wrong key → breadth missing → axis absent / forced default. missing_axes uses
    # the CANONICAL axis name so the behavioral weight loop actually excludes it.
    assert "breadth_state" in under_stem.missing_axes
    # Correct key → real breadth consumed.
    assert "breadth_state" not in under_key.missing_axes
    assert under_key.breadth_state == "STRONG"
