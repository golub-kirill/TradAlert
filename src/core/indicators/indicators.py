"""
Pure indicator functions on pandas Series / DataFrame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas import Series


def atr(df: pd.DataFrame, period: int = 14) -> Series:
    """
    Average True Range (Wilder smoothing).

    Parameters
    ----------
    df     : DataFrame with 'high', 'low', 'close' columns (lowercase).
    period : Lookback window. Default 14.

    Returns
    -------
    Series
        ATR values aligned to df.index, named 'atr'.
        First (period - 1) values are NaN.
    """
    _df   = df.rename(columns=str.lower)
    high  = _df["high"]
    low   = _df["low"]
    close = _df["close"]

    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return (
        true_range
        .ewm(alpha=1 / period, min_periods=period, adjust=False)
        .mean()
        .rename("atr")
    )


def rsi(close: Series, period: int = 14) -> Series:
    """
    Relative Strength Index (Wilder smoothing).

    Parameters
    ----------
    close  : Closing price Series.
    period : Lookback window. Default 14.

    Returns
    -------
    Series
        RSI values in [0, 100], named 'rsi'. First (period - 1) values are NaN.
        Sustained zero-loss windows yield 100.0 rather than NaN.
    """
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs        = avg_gain / avg_loss.where(avg_loss > 1e-10, np.nan)
    rsi_value = 100 - 100 / (1 + rs)
    rsi_value = rsi_value.where(avg_loss > 1e-10, 100.0)
    rsi_value = rsi_value.where(avg_loss.notna(), np.nan)

    return rsi_value.rename("rsi")


def macd(
        close: Series,
    fast:   int = 12,
    slow:   int = 26,
    signal: int = 9,
) -> tuple[Series, Series, Series]:
    """
    MACD line, signal line, and histogram.

    Parameters
    ----------
    close  : Closing price Series.
    fast   : Fast EMA period.  Default 12.
    slow   : Slow EMA period.  Default 26.
    signal : Signal EMA period. Default 9.

    Returns
    -------
    macd_line   : Series  EMA(fast) − EMA(slow), named 'macd'.
    signal_line : Series  EMA(macd_line, signal), named 'macd_signal'.
    histogram   : Series  macd_line − signal_line, named 'macd_hist'.
                  First (slow + signal - 2) values are NaN.
    """
    ema_fast = close.ewm(span=fast, min_periods=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, min_periods=slow, adjust=False).mean()

    macd_line   = (ema_fast - ema_slow).rename("macd")
    signal_line = (
        macd_line
        .ewm(span=signal, min_periods=signal, adjust=False)
        .mean()
        .rename("macd_signal")
    )
    histogram = (macd_line - signal_line).rename("macd_hist")

    return macd_line, signal_line, histogram


def bollinger_bands(
        close: Series,
    period: int   = 20,
    n_std:  float = 2.0,
) -> pd.DataFrame:
    """
    Bollinger Bands — middle, upper, lower, bandwidth, and Z-score.

    Parameters
    ----------
    close  : Closing price Series.
    period : Lookback window for SMA and standard deviation. Default 20.
    n_std  : Band width in standard deviations. Default 2.0.

    Returns
    -------
    pd.DataFrame
        Columns: bb_mid, bb_upper, bb_lower, bb_bw, bb_z
        bb_mid   : SMA(period)
        bb_upper : bb_mid + n_std × σ
        bb_lower : bb_mid − n_std × σ
        bb_bw    : (bb_upper − bb_lower) / bb_mid × 100  (bandwidth %)
        bb_z     : (close − bb_mid) / σ
        First (period − 1) rows are NaN. Uses population std (ddof=0).
    """
    sma   = close.rolling(period, min_periods=period).mean()
    sigma = close.rolling(period, min_periods=period).std(ddof=0)

    upper = sma + n_std * sigma
    lower = sma - n_std * sigma
    bw    = (upper - lower) / sma.where(sma > 0, np.nan) * 100
    z     = (close - sma) / sigma.where(sigma > 1e-10, np.nan)

    return pd.DataFrame({
        "bb_mid":   sma.rename("bb_mid"),
        "bb_upper": upper.rename("bb_upper"),
        "bb_lower": lower.rename("bb_lower"),
        "bb_bw":    bw.rename("bb_bw"),
        "bb_z":     z.rename("bb_z"),
    }, index=close.index)
