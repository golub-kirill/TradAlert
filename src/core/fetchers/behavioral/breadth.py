"""
S&P 500 breadth and sector rotation fetchers (Phase 7).

Computes:
- pct_above_ma200 : % of S&P 500 constituents trading above their MA200.
- sector_rotation : (XLI + XLF) / (XLP + XLU) growth-vs-defensive ratio.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from core.paths import BEHAVIORAL_DIR

import pandas as pd

from core.fetchers.sp500_constituents import get_sp500_constituents
from persistence.cache import load as cache_load

logger = logging.getLogger(__name__)

_DATA_DIR = BEHAVIORAL_DIR
_DEFAULT_STALENESS_HOURS = 24


def compute_sp500_breadth(
        data_dir: Path | str | None = None,
        staleness_hours: int = _DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> pd.DataFrame:
    """Compute % of S&P 500 above 200-day MA.

    Returns
    -------
    DataFrame with DatetimeIndex and ``pct_above_ma200`` column. Empty on
    insufficient data.
    """
    data_dir = Path(data_dir) if data_dir else _DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir / "sp500_breadth.parquet"
    meta_path = data_dir / "sp500_breadth.meta.json"

    if not force and _cache_fresh(meta_path, staleness_hours / 24):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[breadth] loaded from cache (%d rows)", len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[breadth] cache read failed: %s", exc, exc_info=True)

    constituents = get_sp500_constituents()
    if not constituents:
        logger.warning("[breadth] no S&P 500 constituents available")
        return _load_cached_or_empty(parquet_path)

    all_dates = set()
    ticker_ma200 = {}
    for ticker in constituents[:100]:  # see TODO: full universe
        try:
            df = cache_load(ticker)
        except (FileNotFoundError, OSError, ValueError) as exc:
            logger.debug("[breadth] skip %s — cache_load failed: %s", ticker, exc)
            continue
        if len(df) < 200:
            continue
        ma200 = df["close"].rolling(200, min_periods=200).mean()
        above = (df["close"] > ma200).astype(float)
        ticker_ma200[ticker] = above
        all_dates.update(df.index)

    if not ticker_ma200:
        return _load_cached_or_empty(parquet_path)

    all_dates_sorted = sorted(all_dates)
    pct = pd.Series(index=all_dates_sorted, dtype=float)
    for d in all_dates_sorted:
        vals = [s.get(d) for s in ticker_ma200.values() if d in s.index]
        vals = [v for v in vals if pd.notna(v)]
        if vals:
            pct.loc[d] = 100.0 * sum(vals) / len(vals)

    pct = pct.dropna()
    out = pd.DataFrame({"pct_above_ma200": pct})

    try:
        out.to_parquet(parquet_path)
        _write_meta(meta_path)
    except (OSError, ValueError) as exc:
        logger.warning("[breadth] cache write failed: %s", exc, exc_info=True)
    return out


_SECTOR_ETF_PAIRS = {
    "growth": ["XLI", "XLF"],
    "defensive": ["XLP", "XLU"],
}


def compute_sector_rotation(
        data_dir: Path | str | None = None,
        staleness_hours: int = _DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> pd.DataFrame:
    """Compute (XLI+XLF)/(XLP+XLU) growth-vs-defensive ratio.

    Returns DataFrame with ``ratio`` and ``normalized`` columns.
    """
    data_dir = Path(data_dir) if data_dir else _DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir / "sector_ratios.parquet"
    meta_path = data_dir / "sector_ratios.meta.json"

    if not force and _cache_fresh(meta_path, staleness_hours / 24):
        try:
            df = pd.read_parquet(parquet_path)
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[sector] cache read failed: %s", exc, exc_info=True)

    series = {}
    for ticker in _SECTOR_ETF_PAIRS["growth"] + _SECTOR_ETF_PAIRS["defensive"]:
        try:
            series[ticker] = cache_load(ticker)["close"]
        except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
            logger.warning("[sector] missing %s — %s", ticker, exc)
            return _load_cached_or_empty(parquet_path)

    growth = series["XLI"].add(series["XLF"], fill_value=0).dropna()
    defensive = series["XLP"].add(series["XLU"], fill_value=0).dropna()
    common = growth.index.intersection(defensive.index)
    if len(common) < 60:
        return _load_cached_or_empty(parquet_path)

    ratio = (growth.loc[common] / defensive.loc[common]).dropna()
    normalized = ratio / ratio.rolling(252, min_periods=60).mean()

    out = pd.DataFrame({"ratio": ratio, "normalized": normalized})
    try:
        out.to_parquet(parquet_path)
        _write_meta(meta_path)
    except (OSError, ValueError) as exc:
        logger.warning("[sector] cache write failed: %s", exc, exc_info=True)
    return out


def _cache_fresh(meta_path: Path, staleness_days: float) -> bool:
    if not meta_path.exists():
        return False
    try:
        mtime = meta_path.stat().st_mtime
        age_days = (datetime.now().timestamp() - mtime) / 86400
        return age_days < staleness_days
    except (OSError, ValueError) as exc:
        logger.debug("[breadth] cache freshness check failed: %s", exc)
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
            logger.debug("[breadth] cached parquet read failed at %s: %s", parquet_path, exc)
    return pd.DataFrame()
