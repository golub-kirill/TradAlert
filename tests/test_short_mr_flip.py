"""
Phase 10 v2 polish — signal_type-aware short flip.

Mean-reversion shorts fade an overbought rally *near the highs*, so the
"position vs 52-week range" axes (``near_52w_high``, ``far_from_52w_low``)
must keep their long-style sense for MR shorts, while momentum shorts
flip them (they want weakness). All other direction-biased axes flip for
both short flavours.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.filter_engine import MarketRegime
from core.scoring import EntryThresholds, _score_entry

_THR = EntryThresholds()
_FILTERS_CFG = {"events": {"earnings_buffer_days": 5}}
# Include the 52w-range axes (gated on presence in weights).
_WEIGHTS = {
    "trend_up": 1, "breakout_20d": 1, "macd_bullish": 1,
    "near_52w_high": 1, "far_from_52w_low": 1,
}


def _bull_frame(n: int = 260) -> pd.DataFrame:
    close = 100.0 + np.arange(n) * 1.0  # rising → last bar = 52w high
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
            "volume": np.full(n, 1e6), "atr": np.full(n, 1.0),
            "rsi": np.full(n, 55.0), "macd_hist": 0.10 + np.arange(n) * 0.001,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def _comps(direction: str, signal_type: str) -> dict[str, float]:
    _, c = _score_entry(
        _bull_frame(), MarketRegime(trend="BULL", volatility="NORMAL"), None,
        _WEIGHTS, _FILTERS_CFG, _THR, market_dfs=None,
        signal_type=signal_type, direction=direction,
    )
    return c


def test_momentum_short_flips_52w_axes():
    long_c = _comps("long", "momentum")
    mom = _comps("short", "momentum")
    assert mom["near_52w_high"] == pytest.approx(1.0 - long_c["near_52w_high"])
    assert mom["far_from_52w_low"] == pytest.approx(1.0 - long_c["far_from_52w_low"])


def test_mean_rev_short_keeps_52w_axes_longstyle():
    long_c = _comps("long", "momentum")
    mr = _comps("short", "mean_reversion")
    # MR short shorts strength near the highs → 52w axes stay long-style.
    assert mr["near_52w_high"] == pytest.approx(long_c["near_52w_high"])
    assert mr["far_from_52w_low"] == pytest.approx(long_c["far_from_52w_low"])


def test_both_short_flavours_flip_trend_and_macd():
    long_c = _comps("long", "momentum")
    mom = _comps("short", "momentum")
    mr = _comps("short", "mean_reversion")
    for key in ("trend_up", "breakout_20d", "macd_bullish"):
        assert mom[key] == pytest.approx(1.0 - long_c[key]), key
        assert mr[key] == pytest.approx(1.0 - long_c[key]), key
