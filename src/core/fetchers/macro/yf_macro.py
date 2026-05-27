"""
Macro tickers via yfinance.

Fetches OHLCV for macro-relevant symbols and extracts the close price
as a ``value`` series:
 DX-Y.NYB — US Dollar Index (DXY)
 CAD=X — USD/CAD exchange rate
 CL=F — WTI crude oil
 NG=F — Natural gas
 BZ=F — Brent crude (WCS proxy)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from core.paths import MACRO_DIR

logger = logging.getLogger(__name__)

_DEFAULT_STALENESS_HOURS = 24
_DEFAULT_LOOKBACK_DAYS = 365 * 10  # 10 years


def fetch_yf_macro_series(
        ticker: str,
        series_dir: Path | str = MACRO_DIR,
        staleness_hours: int = _DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> pd.DataFrame:
    """
    Fetch a macro ticker via yfinance, cached as parquet.

    Parameters
    ----------
    ticker : yfinance symbol (e.g. ``"CL=F"``).
    series_dir : Directory for parquet files.
    staleness_hours : Re-fetch if cache is older than this.
    force : Always re-fetch, ignoring cache.

    Returns
    -------
    DataFrame with DatetimeIndex and ``value`` column (close price).
    Empty DataFrame on failure (fail-open).
    """
    series_dir = Path(series_dir)
    series_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = series_dir / f"{ticker}.parquet"
    meta_path = series_dir / f"{ticker}.meta.json"

    if not force and _cache_fresh(meta_path, staleness_hours):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[yf_macro] %s loaded from cache (%d rows)", ticker, len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[yf_macro] cache read failed for %s: %s", ticker, exc, exc_info=True)

    start = (datetime.now() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(start=start, auto_adjust=False)
    except (OSError, ValueError, AttributeError, RuntimeError) as exc:
        # yfinance wraps network and parse errors broadly; cover OS/value
        # for the common cases and Attribute/Runtime for the upstream wraps.
        logger.warning("[yf_macro] fetch failed for %s: %s", ticker, exc, exc_info=True)
        return _load_cached_or_empty(parquet_path)

    if df.empty:
        logger.warning("[yf_macro] no data returned for %s", ticker)
        return _load_cached_or_empty(parquet_path)

    result = pd.DataFrame({"value": df["Close"]})
    result.index.name = None

    try:
        result.to_parquet(parquet_path)
        _write_meta(meta_path)
        logger.info("[yf_macro] %s fetched and cached (%d rows)", ticker, len(result))
    except (OSError, ValueError) as exc:
        logger.warning("[yf_macro] cache write failed for %s: %s", ticker, exc, exc_info=True)

    return result


def _cache_fresh(meta_path: Path, staleness_hours: int) -> bool:
    if not meta_path.exists():
        return False
    try:
        mtime = meta_path.stat().st_mtime
        age_hours = (datetime.now().timestamp() - mtime) / 3600
        return age_hours < staleness_hours
    except (OSError, ValueError) as exc:
        logger.debug("[yf_macro] cache freshness check failed: %s", exc)
        return False


def _write_meta(meta_path: Path) -> None:
    import json
    meta = {"fetched_at": datetime.now().isoformat()}
    meta_path.write_text(json.dumps(meta))


def _load_cached_or_empty(parquet_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except (OSError, ValueError) as exc:
            logger.debug("[yf_macro] cached parquet read failed at %s: %s", parquet_path, exc)
    return pd.DataFrame(index=pd.DatetimeIndex([]), columns=["value"])
