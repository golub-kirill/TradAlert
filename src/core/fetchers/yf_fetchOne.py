"""
Single-ticker OHLCV download from Yahoo Finance via yfinance.

Returns a partially standardised DataFrame: lowercase OHLCV columns,
tz-naive DatetimeIndex named 'timestamp'. Full content validation
(OHLCV logic, dtypes, NaN cleanup) is performed by validate_ohlcv()
between this fetch and cache.save() — see cache.get_or_fetch().
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from core.validators.dataframe_validator import REQUIRED_COLUMNS
from core.validators.yf_tickerValidator import validate_ticker

DEFAULT_LOOKBACK: int = 500  # calendar days — ~350 trading days, covers MA200 warmup
DEFAULT_INTERVAL: str = "1d"  # daily bars — correct for swing trading


# ── public API ────────────────────────────────────────────────────────────────

def fetch(
        ticker: str,
        start: str | None = None,
        end: str | None = None,
        interval: str = DEFAULT_INTERVAL,
) -> pd.DataFrame:
    """
    Download OHLCV for *ticker* and return a partially standardised DataFrame.

    Parameters
    ----------
    ticker   : Ticker symbol, e.g. "AAPL", "ZQQ.TO", "BTC-USD".
               Validated and normalised before the network request is made.
    start    : ISO date string, inclusive. Defaults to DEFAULT_LOOKBACK days ago.
    end      : ISO date string, inclusive. Defaults to today.
    interval : yfinance interval string. Default "1d" (daily bars).
               Valid values: 1m 2m 5m 15m 30m 60m 90m 1h 1d 5d 1wk 1mo 3mo.
               Note: intraday intervals cannot extend beyond the last 60 days.

    Returns
    -------
    pd.DataFrame
        Columns : open, high, low, close, volume  (lowercase, ordered)
        Index   : DatetimeIndex named 'timestamp', tz-naive, sorted ascending
        Dtypes  : as returned by yfinance — full dtype enforcement happens in
                  validate_ohlcv() before the frame reaches cache.save().

    Raises
    ------
    FetchError
        When the ticker string is invalid (see yfinance_validator).
    ValueError
        When yfinance returns an empty response for a valid ticker.
    """
    ticker = validate_ticker(ticker)

    # yfinance treats `end` as exclusive — pass tomorrow to include today's bar.
    start = start or (date.today() - timedelta(days=DEFAULT_LOOKBACK)).isoformat()
    end = end or (date.today() + timedelta(days=1)).isoformat()

    raw = yf.download(
        ticker,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=True,
        repair=True,  # detects and fixes 100x currency unit mixups (CAD/USD)
        progress=False,
    )

    if raw.empty:
        raise ValueError(f"yfinance returned no data for '{ticker}'")

    return _standardise(raw)


# ── internal ──────────────────────────────────────────────────────────────────

def _standardise(df: pd.DataFrame) -> pd.DataFrame:
    """
    Flatten the yfinance MultiIndex response into a clean OHLCV frame.

    Steps
    ─────
    1. Collapse MultiIndex columns to the first level
       (e.g. ('Close','AAPL') → 'Close').
    2. Clear the columns name (yfinance sets it to 'Price').
    3. Lowercase and strip column names.
    4. Select and reorder to REQUIRED_COLUMNS — drops any yfinance extras.
    5. Set a tz-naive DatetimeIndex named 'timestamp'.
    6. Sort ascending and drop rows where close is NaN.

    REQUIRED_COLUMNS presence is enforced downstream in validate_ohlcv().
    """
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns.name = None

    df.columns = df.columns.str.lower().str.strip()

    available = [c for c in REQUIRED_COLUMNS if c in df.columns]
    df = df[available].copy()

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = pd.to_datetime(df.index)
    df.index.name = "timestamp"

    df = df.sort_index()
    df = df.dropna(subset=["close"])

    return df
