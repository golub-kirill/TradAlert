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

from core.fetchers import cache_meta
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

    if not force and cache_meta.is_fresh(meta_path, staleness_hours * 3600):
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
        # yfinance wraps network/parse errors broadly — catch the common OS/value
        # cases plus the Attribute/Runtime upstream wraps.
        logger.warning("[yf_macro] fetch failed for %s: %s", ticker, exc, exc_info=True)
        return _load_cached_or_empty(parquet_path, staleness_hours)

    if df.empty:
        logger.warning("[yf_macro] no data returned for %s", ticker)
        return _load_cached_or_empty(parquet_path, staleness_hours)

    result = pd.DataFrame({"value": df["Close"]})
    result.index.name = None

    try:
        result.to_parquet(parquet_path)
        cache_meta.write_meta(meta_path)
        logger.info("[yf_macro] %s fetched and cached (%d rows)", ticker, len(result))
    except (OSError, ValueError) as exc:
        logger.warning("[yf_macro] cache write failed for %s: %s", ticker, exc, exc_info=True)

    return result

def _load_cached_or_empty(parquet_path: Path,
                          staleness_hours: float = _DEFAULT_STALENESS_HOURS) -> pd.DataFrame:
    """Serve cached parquet (fail-open) when a fetch fails; WARN with cache age when
    past the staleness window so a stale cache can't masquerade as fresh (audit F2)."""
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            age = cache_meta.age_seconds(parquet_path)
            if age is not None and age > staleness_hours * 3600:
                logger.warning(
                    "[yf_macro] serving STALE cache for %s — %.1f h old (> %g h "
                    "window); upstream fetch failed, value may be outdated.",
                    parquet_path.stem, age / 3600.0, staleness_hours,
                )
            return df
        except (OSError, ValueError) as exc:
            logger.debug("[yf_macro] cached parquet read failed at %s: %s", parquet_path, exc)
    return pd.DataFrame(index=pd.DatetimeIndex([]), columns=["value"])
