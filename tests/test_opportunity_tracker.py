"""Pure-math helpers for scripts/opportunity_tracker.py (no DB / no network).

Covers the market-adjusted forward-return geometry, the two-sided classifier,
and the per-gate aggregation that feed the opportunity-cost readout. The DB and
price I/O live in main() and are not exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import opportunity_tracker as ot  # noqa: E402


def _series(close: np.ndarray, dates: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(close, index=dates).sort_index()


# ── forward_returns ─────────────────────────────────────────────────────────

def test_forward_returns_flat_spy_equals_raw():
    # 40 bars; SPY perfectly flat → market adjustment is a no-op (mkt_adj == raw).
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.linspace(100.0, 139.0, n)          # smooth uptrend
    spy = _series(np.full(n, 500.0), dates)        # flat benchmark
    i0 = 5
    out = ot.forward_returns(close, dates, spy, i0, horizons=(5, 21))
    raw5 = close[i0 + 5] / close[i0] - 1.0
    raw21 = close[i0 + 21] / close[i0] - 1.0
    assert out["fwd5"] == pytest.approx(raw5)
    assert out["fwd21"] == pytest.approx(raw21)


def test_forward_returns_plus_10pct_at_21():
    # Ticker +10% at i0+21 over flat SPY → fwd21 ≈ +0.10.
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    i0 = 2
    close[i0 + 21] = 110.0                          # exactly +10% at the 21-bar mark
    spy = _series(np.full(n, 300.0), dates)         # flat benchmark
    out = ot.forward_returns(close, dates, spy, i0, horizons=(5, 21))
    assert out["fwd21"] == pytest.approx(0.10)


def test_forward_returns_market_adjust_subtracts_spy():
    # Ticker +10% but SPY +4% over the same span → mkt-adj ≈ +6%.
    n = 30
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    spy_vals = np.full(n, 200.0)
    i0 = 1
    close[i0 + 21] = 110.0
    spy_vals[i0 + 21] = 208.0                       # +4% benchmark over the span
    out = ot.forward_returns(close, dates, _series(spy_vals, dates), i0, horizons=(21,))
    assert out["fwd21"] == pytest.approx(0.10 - 0.04)


def test_forward_returns_past_end_is_nan():
    # i0+h runs past the end of the series → NaN for that horizon.
    n = 10
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    spy = _series(np.full(n, 100.0), dates)
    out = ot.forward_returns(close, dates, spy, i0=3, horizons=(5, 21))
    assert out["fwd5"] == pytest.approx(0.0)        # 3+5=8 < 10 → valid
    assert np.isnan(out["fwd21"])                   # 3+21=24 ≥ 10 → NaN


def test_forward_returns_mdd21_when_window_exists():
    # A dip to 90 then recovery inside the 21-window → mdd21 == -0.10.
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    i0 = 2
    close[i0 + 5] = 90.0                             # -10% trough inside the window
    spy = _series(np.full(n, 100.0), dates)
    out = ot.forward_returns(close, dates, spy, i0, horizons=(21,))
    assert out["mdd21"] == pytest.approx(-0.10)


# ── classify ────────────────────────────────────────────────────────────────

def test_classify_missed_winner():
    assert ot.classify(0.08, win=0.05, lose=0.05) == "missed_winner"


def test_classify_avoided_loser():
    assert ot.classify(-0.08, win=0.05, lose=0.05) == "avoided_loser"


def test_classify_neutral():
    assert ot.classify(0.01, win=0.05, lose=0.05) == "neutral"


def test_classify_nan_is_neutral():
    assert ot.classify(float("nan")) == "neutral"


# ── aggregate ───────────────────────────────────────────────────────────────

def test_aggregate_counts_and_net_sign():
    obs = [
        {"gate": "rsi", "fwd5": 0.02, "fwd21": 0.08, "cls": "missed_winner"},
        {"gate": "rsi", "fwd5": -0.03, "fwd21": -0.10, "cls": "avoided_loser"},
        {"gate": "rsi", "fwd5": 0.0, "fwd21": 0.01, "cls": "neutral"},
        {"gate": "trend", "fwd5": -0.05, "fwd21": -0.12, "cls": "avoided_loser"},
    ]
    out = ot.aggregate(obs)

    rsi = out["rsi"]
    assert rsi["n"] == 3
    assert rsi["pct_missed_winner"] == pytest.approx(100 / 3)
    assert rsi["pct_avoided_loser"] == pytest.approx(100 / 3)
    # mean fwd21 = (0.08 - 0.10 + 0.01)/3 = -0.0033... → net negative (avoided losers)
    assert rsi["net"] == pytest.approx((0.08 - 0.10 + 0.01) / 3)
    assert rsi["net"] < 0

    trend = out["trend"]
    assert trend["n"] == 1
    assert trend["pct_avoided_loser"] == pytest.approx(100.0)
    assert trend["net"] == pytest.approx(-0.12)

    allg = out["__ALL__"]
    assert allg["n"] == 4
    assert allg["net"] == pytest.approx((0.08 - 0.10 + 0.01 - 0.12) / 4)
    assert allg["net"] < 0


def test_aggregate_drops_nan_fwd21_from_stats():
    obs = [
        {"gate": "g", "fwd5": 0.01, "fwd21": 0.08, "cls": "missed_winner"},
        {"gate": "g", "fwd5": 0.01, "fwd21": float("nan"), "cls": "neutral"},
    ]
    out = ot.aggregate(obs)
    g = out["g"]
    assert g["n"] == 1                              # NaN fwd21 dropped from numeric stats
    assert g["mean_fwd21"] == pytest.approx(0.08)
    # but % uses the full row count (denominator includes the NaN row)
    assert g["pct_missed_winner"] == pytest.approx(50.0)


def test_aggregate_net_positive_when_cost_you():
    obs = [
        {"gate": "g", "fwd5": 0.05, "fwd21": 0.15, "cls": "missed_winner"},
        {"gate": "g", "fwd5": 0.04, "fwd21": 0.09, "cls": "missed_winner"},
    ]
    out = ot.aggregate(obs)["g"]
    assert out["net"] > 0                           # positive ⇒ the gate cost you
    assert out["pct_missed_winner"] == pytest.approx(100.0)
