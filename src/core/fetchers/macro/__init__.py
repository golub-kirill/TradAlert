"""
Macro data fetchers — FRED, BoC Valet, and yfinance macro tickers.

Each fetcher follows the same contract as the existing OHLCV fetchers:
 fetch_series(series_id, staleness_hours, force) -> pd.DataFrame

Returns a DataFrame with a DatetimeIndex and a ``value`` column.
Monthly series (CPI, PCE) also carry a ``release_date`` column.

Fail-open: if a fetcher fails, it logs a WARNING and returns the cached
series if available, or an empty DataFrame otherwise.

Public API
----------
fetch_all_macro_series(settings) -> dict[str, pd.DataFrame]
fetch_fred_series(series_id, ...) -> pd.DataFrame
fetch_boc_series(series_id, ...) -> pd.DataFrame
fetch_yf_macro_series(ticker, ...) -> pd.DataFrame
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import yaml

from core.fetchers.macro.boc import fetch_boc_series
from core.fetchers.macro.fred import fetch_fred_series
from core.fetchers.macro.yf_macro import fetch_yf_macro_series

logger = logging.getLogger(__name__)

__all__ = [
    "fetch_all_macro_series",
    "fetch_fred_series",
    "fetch_boc_series",
    "fetch_yf_macro_series",
]


def fetch_all_macro_series(
        settings_path: str | Path = "config/settings.yaml",
        force: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Fetch all configured macro series in one call.

    Parameters
    ----------
    settings_path : Path to settings.yaml.
    force : Always re-fetch, ignoring cache.

    Returns
    -------
    dict mapping series_id/ticker to its DataFrame.
    Missing series are simply absent from the dict (fail-open).
    """
    if isinstance(settings_path, str):
        settings_path = Path(settings_path)

    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    macro_cfg = settings.get("macro", {})
    if not macro_cfg.get("enabled", True):
        logger.info("[macro] macro layer disabled; skipping fetch")
        return {}

    series_dir = macro_cfg.get("series_dir", "data/macro")
    staleness = macro_cfg.get("staleness_hours", 24)
    subset = macro_cfg.get("series_subset")

    all_series: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    # FRED series
    for sid in macro_cfg.get("fred_series", []):
        if subset and sid not in subset:
            continue
        df = fetch_fred_series(sid, series_dir, staleness, force)
        if not df.empty:
            all_series[sid] = df
        else:
            failed.append(f"fred:{sid}")

    # BoC series
    for sid in macro_cfg.get("boc_series", []):
        if subset and sid not in subset:
            continue
        df = fetch_boc_series(sid, series_dir, staleness, force)
        if not df.empty:
            all_series[sid] = df
        else:
            failed.append(f"boc:{sid}")

    # yfinance macro tickers
    for ticker in macro_cfg.get("yf_series", []):
        if subset and ticker not in subset:
            continue
        df = fetch_yf_macro_series(ticker, series_dir, staleness, force)
        if not df.empty:
            all_series[ticker] = df
        else:
            failed.append(f"yf:{ticker}")

    total_attempted = len(all_series) + len(failed)
    if failed:
        logger.warning(
            "[macro] %d/%d series fetched, %d failed: %s",
            len(all_series), total_attempted, len(failed), failed,
        )
    else:
        logger.info("[macro] %d/%d series fetched, 0 failed",
                    len(all_series), total_attempted)
    return all_series
