"""
Tests for the scoring-layer direction flip.

Covers the ``direction="short"`` path through the scoring layer:

- ``_score_entry`` inverts the direction-biased components
  (``trend_up``, ``breakout_20d``, ``macd_bullish``, ``ma50_slope``)
  while leaving direction-agnostic components (``volume_spike``,
  ``rsi_healthy``) untouched.
- ``_flip_if_short`` is a no-op for longs and an involution-style
  ``1 - v`` inversion for shorts.
- ``SignalScorer.enrich`` dispatches a ``direction="short"`` signal
  through the flipped path so the attached ``score_components`` are
  short-correct.

The synthetic frames are deterministic: a monotonically rising series
("bullish") and its mirror ("bearish"). Long-style scores on the
bullish frame should be high; flipping to short should drive the
direction-biased ones low, and vice-versa on the bearish frame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.filter_engine import MarketRegime, SignalResult
from core.scoring import (
    EntryThresholds,
    SignalScorer,
    _flip_if_short,
    _score_entry,
)

# Components that must flip for shorts, and controls that must not.
_FLIPPED = ("trend_up", "breakout_20d", "macd_bullish", "ma50_slope")
_UNFLIPPED = ("volume_spike", "rsi_healthy")

_WEIGHTS = {
    "trend_up": 3,
    "ma50_slope": 1,
    "breakout_20d": 2,
    "macd_bullish": 2,
    "volume_spike": 1,
    "rsi_healthy": 1,
}

_FILTERS_CFG = {"events": {"earnings_buffer_days": 5}}
_THR = EntryThresholds()


# ─── synthetic frames ─────────────────────────────────────────────────────────


def _frame(direction: str, n: int = 260) -> pd.DataFrame:
    """Deterministic OHLCV+indicator frame.

    ``direction="bull"`` → monotonically rising close, positive and
    growing MACD histogram. ``direction="bear"`` → the mirror image.
    Long enough (260 bars) to populate MA50/MA200.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    if direction == "bull":
        close = 100.0 + np.arange(n) * 1.0  # rising
        macd_hist = 0.10 + np.arange(n) * 0.001  # positive, growing
    elif direction == "bear":
        close = 100.0 + (n - 1) * 1.0 - np.arange(n) * 1.0  # falling
        macd_hist = -0.10 - np.arange(n) * 0.001  # negative, shrinking
    else:  # pragma: no cover - guard
        raise ValueError(direction)

    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
            "atr": np.full(n, 1.0),
            "rsi": np.full(n, 55.0),  # inside the healthy band
            "macd_hist": macd_hist,
        },
        index=idx,
    )


def _regime() -> MarketRegime:
    return MarketRegime(trend="BULL", volatility="NORMAL")


def _components(df: pd.DataFrame, direction: str) -> dict[str, float]:
    _, comps = _score_entry(
        df,
        _regime(),
        None,  # earnings_date
        _WEIGHTS,
        _FILTERS_CFG,
        _THR,
        market_dfs=None,
        signal_type="momentum",
        direction=direction,
    )
    return comps


# ─── _score_entry flip on a bullish frame ─────────────────────────────────────


def test_bullish_long_scores_high():
    comps = _components(_frame("bull"), "long")
    assert comps["trend_up"] == 1.0
    assert comps["breakout_20d"] == 1.0
    assert comps["macd_bullish"] >= 0.6  # positive & growing → 1.0
    assert comps["ma50_slope"] >= 0.6  # MA50 rising hard


def test_bullish_short_flips_low():
    long_c = _components(_frame("bull"), "long")
    short_c = _components(_frame("bull"), "short")
    for key in _FLIPPED:
        assert short_c[key] == pytest.approx(1.0 - long_c[key], abs=1e-9), key
    # Flipped direction-biased components are now low.
    assert short_c["trend_up"] == 0.0
    assert short_c["breakout_20d"] == 0.0
    assert short_c["macd_bullish"] <= 0.4


def test_unflipped_components_are_direction_agnostic():
    long_c = _components(_frame("bull"), "long")
    short_c = _components(_frame("bull"), "short")
    for key in _UNFLIPPED:
        assert short_c[key] == long_c[key], key


# ─── _score_entry flip on a bearish frame (mirror image) ──────────────────────


def test_bearish_short_scores_high():
    long_c = _components(_frame("bear"), "long")
    short_c = _components(_frame("bear"), "short")
    # Long-style on a falling market is bad...
    assert long_c["trend_up"] == 0.0
    assert long_c["breakout_20d"] == 0.0
    # ...so the short flip makes the direction-biased components good.
    assert short_c["trend_up"] == 1.0
    assert short_c["breakout_20d"] == 1.0
    for key in _FLIPPED:
        assert short_c[key] == pytest.approx(1.0 - long_c[key], abs=1e-9), key


# ─── _flip_if_short unit behavior ─────────────────────────────────────────────


def test_flip_if_short_noop_for_long():
    comps = {"trend_up": 0.8, "breakout_20d": 0.2, "volume_spike": 0.5}
    before = dict(comps)
    _flip_if_short(comps, "long")
    assert comps == before


def test_flip_if_short_inverts_only_listed_keys():
    comps = {"trend_up": 0.8, "volume_spike": 0.3, "rsi_healthy": 0.9}
    _flip_if_short(comps, "short")
    assert comps["trend_up"] == pytest.approx(0.2)  # in flip list
    assert comps["volume_spike"] == 0.3  # not in flip list
    assert comps["rsi_healthy"] == 0.9  # not in flip list


def test_flip_if_short_clamps_range():
    comps = {"trend_up": 1.0, "breakout_20d": 0.0}
    _flip_if_short(comps, "short")
    assert comps["trend_up"] == 0.0
    assert comps["breakout_20d"] == 1.0


# ─── enrich() end-to-end dispatch ─────────────────────────────────────────────


def _scorer() -> SignalScorer:
    settings = {
        "scanner": {
            "weights": _WEIGHTS,
            "exit_weights": {},
            "min_score_to_alert": 60,
        },
        "market_hours": {},
    }
    return SignalScorer(settings, _FILTERS_CFG)


def _enrich_components(direction: str) -> dict[str, float]:
    scorer = _scorer()
    sig = SignalResult(passed=True, direction=direction, signal_type="momentum")
    scorer.enrich(sig, _frame("bull"), _regime())
    return sig.score_components


def test_enrich_short_dispatches_flipped_path():
    long_comps = _enrich_components("long")
    short_comps = _enrich_components("short")
    assert short_comps, "enrich must populate score_components"
    for key in _FLIPPED:
        assert short_comps[key] == pytest.approx(1.0 - long_comps[key], abs=1e-9), key


def test_enrich_short_lowers_overall_score_on_bullish_data():
    scorer = _scorer()
    long_sig = SignalResult(passed=True, direction="long", signal_type="momentum")
    short_sig = SignalResult(passed=True, direction="short", signal_type="momentum")
    scorer.enrich(long_sig, _frame("bull"), _regime())
    scorer.enrich(short_sig, _frame("bull"), _regime())
    # A long-friendly (bullish) frame must score the short lower.
    assert short_sig.score < long_sig.score
