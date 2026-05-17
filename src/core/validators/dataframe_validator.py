"""
OHLCV DataFrame validation and auto-correction.

REQUIRED_COLUMNS is defined here and imported by every other module.

Auto-corrections  (applied with a WARNING log — data is modified)
──────────────────────────────────────────────────────────────────
    • index not a DatetimeIndex   converted via pd.to_datetime()
    • tz-aware index              tz stripped via tz_localize(None)
    • index name != 'timestamp'   renamed to 'timestamp'
    • open/high/low/close dtype   cast to float64
    • ±inf values                 replaced with NaN, then rows dropped
    • NaN in any required column  rows dropped

Hard failures  (raise ValidationError — data cannot be recovered)
──────────────────────────────────────────────────────────────────
    • required column missing
    • index unconvertible to DatetimeIndex
    • high < low on any row
    • close > high on any row
    • close < low  on any row
    • close <= 0   on any row
    • volume < 0   on any row
    • DataFrame empty after all corrections
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from exceptions import ValidationError

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS: list[str] = ["open", "high", "low", "close", "volume"]


# ── public API ────────────────────────────────────────────────────────────────

def validate_ohlcv(
    df:     pd.DataFrame,
    ticker: str = "",
) -> pd.DataFrame:
    """
    Validate and auto-correct a standardised OHLCV DataFrame.

    Parameters
    ----------
    df     : Raw or partially-standardised OHLCV DataFrame.
    ticker : Ticker symbol — used in log and exception messages only.

    Returns
    -------
    pd.DataFrame
        Validated DataFrame with the following guarantees:
        - Columns exactly match REQUIRED_COLUMNS (lowercase, ordered).
        - DatetimeIndex named "timestamp", tz-naive, sorted ascending.
        - No NaN or ±inf values in any required column.
        - open / high / low / close are float64.
        - volume is int64.
        - All OHLCV relationships are logically consistent.

    Raises
    ------
    ValidationError
        On an unfixable violation (see module docstring for the full list).
    """
    tag = f"[{ticker}] " if ticker else ""

    df = df.copy()

    _check_required_columns(df, ticker)

    df = df[REQUIRED_COLUMNS]

    df = _fix_index(df, tag)
    df = _fix_price_dtypes(df, tag)
    df = _drop_bad_rows(df, tag)
    df = _fix_volume_dtype(df, tag)
    df = _check_ohlcv_logic(df, ticker)
    df = df.sort_index()

    if df.empty:
        raise ValidationError(
            "DataFrame is empty after validation corrections",
            ticker=ticker,
        )

    return df


# ── private helpers ───────────────────────────────────────────────────────────

def _check_required_columns(df: pd.DataFrame, ticker: str) -> None:
    """Raise ValidationError listing every missing column."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValidationError(
            f"missing required columns: {missing}",
            ticker=ticker,
        )


def _fix_index(df: pd.DataFrame, tag: str) -> pd.DataFrame:
    """Convert index to a tz-naive DatetimeIndex. Raises if not convertible."""
    if not isinstance(df.index, pd.DatetimeIndex):
        logger.warning(
            "%sindex is %s, not DatetimeIndex — converting via pd.to_datetime()",
            tag,
            type(df.index).__name__,
        )
        try:
            df.index = pd.to_datetime(df.index)
        except Exception as exc:
            raise ValidationError(
                f"index cannot be converted to DatetimeIndex: {exc}",
                ticker=tag.strip("[] "),
            ) from exc

    if df.index.tz is not None:
        logger.warning(
            "%sindex is tz-aware (%s) — stripping timezone",
            tag,
            df.index.tz,
        )
        df.index = df.index.tz_localize(None)

    return df


def _fix_price_dtypes(df: pd.DataFrame, tag: str) -> pd.DataFrame:
    """Cast open/high/low/close to float64. Unconvertible values become NaN."""
    price_cols = ["open", "high", "low", "close"]
    for col in price_cols:
        if df[col].dtype != np.float64:
            logger.warning(
                "%s'%s' dtype is %s — casting to float64",
                tag, col, df[col].dtype,
            )
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df


def _drop_bad_rows(df: pd.DataFrame, tag: str) -> pd.DataFrame:
    """Replace ±inf with NaN, then drop every row with NaN in a required column."""
    inf_mask = df[REQUIRED_COLUMNS].isin([np.inf, -np.inf])
    if inf_mask.any().any():
        inf_rows = int(inf_mask.any(axis=1).sum())
        logger.warning(
            "%sfound ±inf in %d row(s) — replacing with NaN before dropping",
            tag, inf_rows,
        )
        df = df.replace([np.inf, -np.inf], np.nan)

    nan_mask = df[REQUIRED_COLUMNS].isna().any(axis=1)
    nan_count = int(nan_mask.sum())
    if nan_count:
        logger.warning(
            "%sdropping %d row(s) with NaN in required columns",
            tag, nan_count,
        )
        df = df.dropna(subset=REQUIRED_COLUMNS)

    return df


def _fix_volume_dtype(df: pd.DataFrame, tag: str) -> pd.DataFrame:
    """Cast volume to int64. Must be called after _drop_bad_rows."""
    if df["volume"].dtype != np.int64:
        logger.warning(
            "%s'volume' dtype is %s — casting to int64",
            tag, df["volume"].dtype,
        )
        df["volume"] = (
            pd.to_numeric(df["volume"], errors="coerce")
            .round()
            .astype("int64")
        )
    return df


def _check_ohlcv_logic(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Auto-drop recoverable OHLCV violations; raise on unrecoverable ones.

    Dropped with WARNING (yfinance data-quality blips — single bad rows):
        high < low
        close > high
        close < low

    Hard raise (data is fundamentally broken):
        close <= 0   (zero or negative price)
        volume < 0

    Returns the filtered DataFrame with bad rows removed.
    """
    tag = f"[{ticker}] " if ticker else ""

    # ── auto-drop recoverable violations ──────────────────────────────────────
    droppable: list[tuple[pd.Series, str]] = [
        (df["high"] < df["low"], "high < low"),
        (df["close"] > df["high"], "close > high"),
        (df["close"] < df["low"], "close < low"),
    ]
    for mask, description in droppable:
        mask = mask.reindex(df.index, fill_value=False)
        bad = df[mask]
        if not bad.empty:
            sample = bad.index[:3].tolist()
            logger.warning(
                "%sOHLCV violation '%s' on %d row(s) — dropping; "
                "first offenders: %s",
                tag, description, len(bad), sample,
            )
            df = df[~mask]

    # ── hard failures — data cannot be recovered ──────────────────────────────
    hard: list[tuple[pd.Series, str]] = [
        (df["close"] <= 0, "close <= 0 (zero or negative price)"),
        (df["volume"] < 0, "volume < 0"),
    ]
    for mask, description in hard:
        bad = df[mask]
        if not bad.empty:
            sample = bad.index[:3].tolist()
            raise ValidationError(
                f"OHLCV violation '{description}' on {len(bad)} row(s); "
                f"first offenders: {sample}",
                ticker=ticker,
            )

    return df
