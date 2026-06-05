"""
Exact-value unit tests for the core math the headline numbers depend on
(Phase 7 gap: indicators, stats_utils, OHLCV validator had no coverage).
References are recomputed independently or hand-derived — not mirrored from
the implementation — so a formula change breaks the test.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtest.stats_utils import (
    kelly_fraction, sharpe_ratio, sortino_ratio,
    drawdown_series, max_drawdown, _profit_factor,
)
from core.indicators.indicators import atr, rsi, macd, bollinger_bands
from core.validators.dataframe_validator import validate_ohlcv
from exceptions import ValidationError


# ── indicators ────────────────────────────────────────────────────────────────

def _ohlc(close, high=None, low=None, n=None):
    close = pd.Series(close, dtype=float)
    idx = pd.date_range("2020-01-01", periods=len(close), freq="B")
    close.index = idx
    return close


def test_rsi_flat_series_is_50():
    close = _ohlc([100.0] * 40)
    assert rsi(close, 14).iloc[-1] == pytest.approx(50.0)


def test_rsi_monotonic_rally_is_100():
    close = _ohlc([100.0 + i for i in range(40)])  # strictly rising → no losses
    assert rsi(close, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_warmup_is_nan():
    close = _ohlc([100.0, 101.0, 102.0])  # < period
    assert math.isnan(rsi(close, 14).iloc[-1])


def test_atr_constant_true_range_converges():
    # high-low == 2 every bar, no gaps → TR == 2 → Wilder ATR → 2.0
    n = 60
    base = pd.Series(np.full(n, 100.0))
    df = pd.DataFrame({"high": base + 1.0, "low": base - 1.0, "close": base})
    df.index = pd.date_range("2020-01-01", periods=n, freq="B")
    assert atr(df, 14).iloc[-1] == pytest.approx(2.0, abs=1e-9)


def test_macd_structural_identity():
    close = _ohlc(list(np.linspace(50, 150, 80)) + list(np.linspace(150, 120, 20)))
    line, sig, hist = macd(close)
    ema_fast = close.ewm(span=12, min_periods=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, min_periods=26, adjust=False).mean()
    assert line.iloc[-1] == pytest.approx((ema_fast - ema_slow).iloc[-1])
    assert hist.iloc[-1] == pytest.approx((line - sig).iloc[-1])
    assert sig.iloc[-1] == pytest.approx(
        line.ewm(span=9, min_periods=9, adjust=False).mean().iloc[-1])


def test_bollinger_bands_match_definition():
    close = _ohlc(list(np.linspace(50, 90, 60)))
    bb = bollinger_bands(close, 20, 2.0)
    sma = close.rolling(20, min_periods=20).mean()
    sigma = close.rolling(20, min_periods=20).std(ddof=0)
    assert bb["bb_mid"].iloc[-1] == pytest.approx(sma.iloc[-1])
    assert bb["bb_upper"].iloc[-1] == pytest.approx(sma.iloc[-1] + 2 * sigma.iloc[-1])
    assert bb["bb_lower"].iloc[-1] == pytest.approx(sma.iloc[-1] - 2 * sigma.iloc[-1])
    assert bb["bb_z"].iloc[-1] == pytest.approx(
        (close.iloc[-1] - sma.iloc[-1]) / sigma.iloc[-1])


# ── stats_utils ───────────────────────────────────────────────────────────────

def test_kelly_known_values():
    k = kelly_fraction(0.5, 2.0, 1.0)  # f = 0.5/1 - 0.5/2 = 0.25
    assert k.full_kelly == pytest.approx(0.25)
    assert k.half_kelly == pytest.approx(0.125)
    assert k.quarter_kelly == pytest.approx(0.0625)
    assert k.edge_per_trade == pytest.approx(0.5)  # 0.5*2 - 0.5*1
    assert k.breakeven_wr == pytest.approx(1.0 / 3.0)  # loss/(win+loss)


def test_kelly_floors_at_zero_for_negative_edge():
    assert kelly_fraction(0.2, 1.0, 1.0).full_kelly == 0.0


def test_drawdown_and_max_drawdown_exact():
    rs = [1.0, -3.0, 2.0]  # equity 1,-2,0 ; peak 1,1,1 ; dd 0,3,1
    assert list(drawdown_series(rs)) == [0.0, 3.0, 1.0]
    assert max_drawdown(rs) == pytest.approx(3.0)


def test_profit_factor_edges():
    assert _profit_factor(np.array([2.0, -1.0])) == pytest.approx(2.0)
    assert _profit_factor(np.array([-1.0, -2.0])) == 0.0  # all losses
    assert _profit_factor(np.array([1.0, 2.0])) == float("inf")  # no losses
    assert math.isnan(_profit_factor(np.array([])))  # no trades


def test_sharpe_nan_on_zero_variance_and_sign_otherwise():
    assert math.isnan(sharpe_ratio([1.0, 1.0, 1.0, 1.0]))  # std == 0 → nan
    pos = sharpe_ratio([0.5, 1.0, -0.2, 0.8, 0.3, 0.6])
    assert math.isfinite(pos) and pos > 0
    assert sortino_ratio([0.5, 1.0, 0.3]) == float("inf")  # no negatives
    assert math.isnan(sharpe_ratio([1.0]))                 # < 2 obs → nan


def test_sharpe_is_rf_zero_scale_invariant():
    # rf = 0 ⇒ Sharpe = mean/std(ddof=1) * sqrt(12), no risk-free hurdle.
    series = [1.0, -2.0, 3.0, 0.5]
    arr = np.array(series)
    expected = arr.mean() / arr.std(ddof=1) * math.sqrt(12)
    assert sharpe_ratio(series) == pytest.approx(expected)
    # Scale-invariant: multiplying every month by a constant leaves Sharpe unchanged.
    assert sharpe_ratio([3.0 * x for x in series]) == pytest.approx(sharpe_ratio(series))


def test_sortino_downside_deviation_is_over_N():
    # dd must average squared shortfall over ALL months (/N), not just down-months.
    series = [1.0, 1.0, 1.0, -1.0]            # one down-month of -1.0, N = 4
    dd = math.sqrt(((-1.0) ** 2) / 4)          # /N: 0.5  (the old /n_down gave 1.0)
    expected = (np.mean(series)) / dd * math.sqrt(12)
    assert sortino_ratio(series) == pytest.approx(expected)
    # Down-month dominates: /N is strictly more generous than the old /n_down form.
    assert sortino_ratio(series) > 0


# ── OHLCV validator ───────────────────────────────────────────────────────────

def _valid_df():
    idx = pd.date_range("2020-01-01", periods=3, freq="B")
    return pd.DataFrame(
        {"open": [10.0, 11, 12], "high": [10.5, 11.5, 12.5],
         "low": [9.5, 10.5, 11.5], "close": [10.2, 11.2, 12.2],
         "volume": [1000, 1100, 1200]},
        index=idx,
    )


def test_validate_passes_clean_frame_and_casts_types():
    out = validate_ohlcv(_valid_df(), "X")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out["close"].dtype == np.float64
    assert out["volume"].dtype == np.int64
    assert len(out) == 3


def test_validate_drops_nan_row():
    df = _valid_df()
    df.loc[df.index[1], "close"] = np.nan
    out = validate_ohlcv(df, "X")
    assert len(out) == 2


def test_validate_raises_on_nonpositive_close():
    # Make the bar internally consistent (low <= close <= high) so it reaches
    # the close<=0 hard-raise instead of being dropped as close<low first.
    df = _valid_df()
    df.loc[df.index[0], ["open", "high", "low", "close"]] = [0.0, 0.5, -0.5, 0.0]
    with pytest.raises(ValidationError):
        validate_ohlcv(df, "X")


def test_validate_raises_on_missing_column():
    df = _valid_df().drop(columns=["volume"])
    with pytest.raises(ValidationError):
        validate_ohlcv(df, "X")
