"""
Parquet cache for OHLCV DataFrames.

Staleness threshold loaded from settings.yaml → ``storage.staleness_hours``
(default 12h when unset).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from core.validators.dataframe_validator import REQUIRED_COLUMNS, validate_ohlcv
from exceptions import ValidationError

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR: Path = Path("data/prices")
# Absolute path so this resolves correctly regardless of the working directory
# at import time (MINOR-02 in TODO — previously used a CWD-relative path).
_SETTINGS_PATH: Path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"


def load_default_staleness_h() -> int:
    """Return ``storage.staleness_hours`` from settings.yaml, else 12."""
    if _SETTINGS_PATH.exists():
        settings = yaml.safe_load(_SETTINGS_PATH.read_text())
        return settings.get("storage", {}).get("staleness_hours", 12)
    return 12


DEFAULT_STALENESS_H: int = load_default_staleness_h()


# ── public API ────────────────────────────────────────────────────────────────

def is_fresh(
        ticker: str,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        staleness_hours: int = DEFAULT_STALENESS_H,
) -> bool:
    """
    Return True when a cache file exists and is younger than staleness_hours.

    Parameters
    ----------
    ticker          : Ticker symbol.
    cache_dir       : Directory that contains parquet files.
    staleness_hours : Max file age in hours before the cache is stale.

    Returns
    -------
    bool
    """
    path = _path(ticker, cache_dir)
    if not path.exists():
        return False

    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    age = datetime.now() - mtime
    fresh = age < timedelta(hours=staleness_hours)

    if not fresh:
        logger.debug(
            "Cache stale  ✗ %s  (%.1fh old, threshold %dh)",
            ticker,
            age.total_seconds() / 3600,
            staleness_hours,
        )

    return fresh


def load(
        ticker: str,
        start: str | None = None,
        end: str | None = None,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
) -> pd.DataFrame:
    """
    Load a cached DataFrame, optionally trimmed to [start, end].

    Parameters
    ----------
    ticker    : Ticker symbol.
    start     : Trim window start, ISO date string (inclusive). Optional.
    end       : Trim window end,   ISO date string (inclusive). Optional.
    cache_dir : Directory that contains parquet files.

    Returns
    -------
    pd.DataFrame
        OHLCV frame with DatetimeIndex named 'timestamp'.

    Raises
    ------
    FileNotFoundError
        When no parquet file exists for the ticker.
    """
    path = _path(ticker, cache_dir)
    if not path.exists():
        raise FileNotFoundError(f"No cache for '{ticker}' at {path}")

    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    logger.debug("Cache load  ← %s  (%d rows)", path, len(df))
    return _trim(df, start, end)


def save(
        df: pd.DataFrame,
        ticker: str,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
) -> None:
    """
    Validate structure and write a standardised OHLCV DataFrame to parquet.

    Parameters
    ----------
    df        : Validated OHLCV DataFrame.
    ticker    : Ticker symbol — used to derive the parquet filename.
    cache_dir : Directory to write the parquet file into.

    Raises
    ------
    ValidationError
        On missing required columns, non-DatetimeIndex, or tz-aware index.
    """
    _validate_structure(df)
    path = _path(ticker, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    logger.debug("Cache save  → %s  (%d rows)", path, len(df))


def get_or_fetch(
        ticker: str,
        fetcher,  # callable(ticker) -> pd.DataFrame
        start: str | None = None,
        end: str | None = None,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        staleness_hours: int = DEFAULT_STALENESS_H,
        force: bool = False,
) -> pd.DataFrame:
    """
    Return cached data when fresh; otherwise fetch, validate, cache, return.

    Pipeline on miss or stale cache:
        fetcher(ticker) → validate_ohlcv() → save() → trimmed frame.

    Parameters
    ----------
    ticker          : Ticker symbol.
    fetcher         : callable(ticker) → standardised DataFrame.
    start           : Trim window start, ISO date string. Optional.
    end             : Trim window end,   ISO date string. Optional.
    cache_dir       : Directory that contains parquet files.
    staleness_hours : Passed to is_fresh().
    force           : When True, skip freshness check and always re-fetch.

    Returns
    -------
    pd.DataFrame

    Raises
    ------
    FetchError        From the fetcher on bad ticker or empty response.
    ValidationError   From validate_ohlcv() on bad OHLCV data.
    """
    if not force and is_fresh(ticker, cache_dir, staleness_hours):
        logger.debug("Cache hit   ✓ %s", ticker)
        return load(ticker, start, end, cache_dir)

    logger.debug("Fetching    ↓ %s", ticker)
    df = fetcher(ticker)
    df = validate_ohlcv(df, ticker=ticker)
    save(df, ticker, cache_dir)
    return _trim(df, start, end)


# ── internals ─────────────────────────────────────────────────────────────────

def _path(ticker: str, cache_dir: Path | str) -> Path:
    return Path(cache_dir) / f"{ticker.upper()}.parquet"


def _trim(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df.loc[start:]
    if end:
        df = df.loc[:end]
    return df


def _validate_structure(df: pd.DataFrame) -> None:
    """
    Structural guard for save(): column presence, DatetimeIndex type, tz-naive index.

    Raises
    ------
    ValidationError
        On missing required columns, non-DatetimeIndex, or tz-aware index.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValidationError(f"DataFrame missing required columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValidationError(f"DataFrame index must be a DatetimeIndex, got {type(df.index).__name__}")
    if df.index.tz is not None:
        raise ValidationError(f"DatetimeIndex must be tz-naive, got tz={df.index.tz}")
