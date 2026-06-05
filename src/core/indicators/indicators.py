"""
Pure indicator functions on pandas Series / DataFrame.

Public helpers
--------------
atr(df) Average True Range (Wilder EMA)
rsi(close) Relative Strength Index (Wilder EMA)
macd(close) MACD line, signal line, histogram
bollinger_bands(close) BB mid/upper/lower/bandwidth/Z-score
attach_indicators(df) Attach all of the above to a copy of df
 (single canonical implementation used by both
 the live pipeline and the backtester)
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
    df : DataFrame with 'high', 'low', 'close' columns (lowercase).
    period : Lookback window. Default 14.

    Returns
    -------
    Series
    ATR values aligned to df.index, named 'atr'.
    First (period - 1) values are NaN.
    """
    _df = df.rename(columns=str.lower)
    high = _df["high"]
    low = _df["low"]
    close = _df["close"]

    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
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
    close : Closing price Series.
    period : Lookback window. Default 14.

    Returns
    -------
    Series
    RSI values in [0, 100], named 'rsi'. First (period - 1) values are NaN.

    Edge cases
    ----------
    avg_loss == 0 AND avg_gain > 0 → 100 (asymmetric rally, no losses)
    avg_loss == 0 AND avg_gain == 0 → 50 (flat market, no momentum either way)
    previously returned 100 on a flat series — that bias caused
    the exit-scorer's rsi_overbought sub-score to fire (rsi=100 > 60+10)
    on quiescent OTC-style series with multiple identical closes in a row.
    avg_loss is NaN (warmup) → NaN
    """
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    eps = 1e-10
    rs = avg_gain / avg_loss.where(avg_loss > eps, np.nan)
    rsi_value = 100 - 100 / (1 + rs)
    # distinguish "no losses but real gains" (→100) from "no losses
    # AND no gains" (flat → 50). Without this guard, a constant price
    # series silently registered RSI=100 ("maximally overbought").
    rsi_value = rsi_value.where(
        ~((avg_loss <= eps) & (avg_gain > eps)), 100.0,
    )
    rsi_value = rsi_value.where(
        ~((avg_loss <= eps) & (avg_gain <= eps)), 50.0,
    )
    rsi_value = rsi_value.where(avg_loss.notna(), np.nan)

    return rsi_value.rename("rsi")


def macd(
        close: Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
) -> tuple[Series, Series, Series]:
    """
    MACD line, signal line, and histogram.

    Parameters
    ----------
    close : Closing price Series.
    fast : Fast EMA period. Default 12.
    slow : Slow EMA period. Default 26.
    signal : Signal EMA period. Default 9.

    Returns
    -------
    macd_line : Series EMA(fast) − EMA(slow), named 'macd'.
    signal_line : Series EMA(macd_line, signal), named 'macd_signal'.
    histogram : Series macd_line − signal_line, named 'macd_hist'.
    First (slow + signal - 2) values are NaN.
    """
    ema_fast = close.ewm(span=fast, min_periods=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, min_periods=slow, adjust=False).mean()

    macd_line = (ema_fast - ema_slow).rename("macd")
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
        period: int = 20,
        n_std: float = 2.0,
) -> pd.DataFrame:
    """
    Bollinger Bands — middle, upper, lower, bandwidth, and Z-score.

    Parameters
    ----------
    close : Closing price Series.
    period : Lookback window for SMA and standard deviation. Default 20.
    n_std : Band width in standard deviations. Default 2.0.

    Returns
    -------
    pd.DataFrame
    Columns: bb_mid, bb_upper, bb_lower, bb_bw, bb_z
    bb_mid : SMA(period)
    bb_upper : bb_mid + n_std × σ
    bb_lower : bb_mid − n_std × σ
    bb_bw : (bb_upper − bb_lower) / bb_mid × 100 (bandwidth %)
    bb_z : (close − bb_mid) / σ
    First (period − 1) rows are NaN. Uses population std (ddof=0).
    """
    sma = close.rolling(period, min_periods=period).mean()
    sigma = close.rolling(period, min_periods=period).std(ddof=0)

    upper = sma + n_std * sigma
    lower = sma - n_std * sigma
    bw = (upper - lower) / sma.where(sma > 0, np.nan) * 100
    z = (close - sma) / sigma.where(sigma > 1e-10, np.nan)

    return pd.DataFrame({
        "bb_mid": sma.rename("bb_mid"),
        "bb_upper": upper.rename("bb_upper"),
        "bb_lower": lower.rename("bb_lower"),
        "bb_bw": bw.rename("bb_bw"),
        "bb_z": z.rename("bb_z"),
    }, index=close.index)


def attach_indicators(
        df: pd.DataFrame,
        ma_fast: int = 50,
        ma_slow: int = 200,
) -> pd.DataFrame:
    """
    Return a copy of *df* with all standard indicator columns attached.

    Added columns
    -------------
    atr, rsi, macd, macd_signal, macd_hist,
    bb_mid, bb_upper, bb_lower, bb_bw, bb_z,
    ma_fast, ma_slow, weekly_sma10

    ``ma_fast``, ``ma_slow`` and ``weekly_sma10`` were previously
    re-computed inside every per-bar engine call via
    ``df["close"].rolling(N).mean.iloc[-1]``. In a single sweep cell that
    is hundreds of thousands of rolling calls (per bar × per ticker × twice
    per signal). Now they live as columns: the hot path reads
    ``row["ma_fast"]`` in O(1).

    Defaults (50 / 200) match the canonical ``filters.yaml::trend`` block.
    Callers that use non-default MA periods (e.g. the sweep engine when
    sweeping ``trend.ma_fast``) must pass them explicitly so columns match
    the engine's configured values.

    Parameters
    ----------
    df : Validated OHLCV DataFrame with columns open/high/low/close/volume.
    ma_fast : Fast MA period (e.g. 50). Stored in column ``ma_fast``.
    ma_slow : Slow MA period (e.g. 200). Stored in column ``ma_slow``.

    Returns
    -------
    pd.DataFrame
    New DataFrame; the original is not mutated.
    """
    df = df.copy()

    df["atr"] = atr(df)
    df["rsi"] = rsi(df["close"])

    macd_line, signal_line, histogram = macd(df["close"])
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = histogram

    bb = bollinger_bands(df["close"])
    df["bb_mid"] = bb["bb_mid"]
    df["bb_upper"] = bb["bb_upper"]
    df["bb_lower"] = bb["bb_lower"]
    df["bb_bw"] = bb["bb_bw"]
    df["bb_z"] = bb["bb_z"]

    # precomputed rolling means consumed by engine/scoring hot loops.
    close = df["close"]
    df["ma_fast"] = close.rolling(ma_fast, min_periods=ma_fast).mean()
    df["ma_slow"] = close.rolling(ma_slow, min_periods=ma_slow).mean()

    # weekly close → 10-week SMA, forward-filled so daily rows can
    # read the latest completed weekly value without re-resampling per call.
    weekly_close = close.resample("W-FRI").last()
    weekly_sma10 = weekly_close.rolling(10, min_periods=10).mean()
    # Align back onto the daily index; ffill within the week so every day
    # carries last completed Friday's value.
    df["weekly_sma10"] = weekly_sma10.reindex(df.index, method="ffill")

    return df
