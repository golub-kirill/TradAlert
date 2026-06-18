"""
S&P 500 breadth and sector rotation fetchers.

Computes:
- pct_above_ma200 : % of S&P 500 constituents trading above their MA200.
- sector_rotation : (XLI + XLF) / (XLP + XLU) growth-vs-defensive ratio.

SURVIVORSHIP CAVEAT (audit F3): ``pct_above_ma200`` is built from the CURRENT
S&P 500 membership applied across each name's full price history. Names that were
removed from the index are absent, and today's members are projected backward, so
historical breadth is biased bullish (only survivors are counted). The honest fix
is a date-stamped historical-membership source; until then the early-history
breadth axis should be read as optimistic. The fetcher emits a WARNING on every
recompute so the limitation is never silent.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from core.paths import BEHAVIORAL_DIR

import pandas as pd

from core.fetchers import cache_meta
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
        return _load_cached_or_empty(parquet_path, staleness_hours)

    # Survivorship bias (audit F3): this is the CURRENT membership applied to the
    # full price history — removed names are missing and present names are
    # projected backward, biasing early-history breadth bullish. A date-stamped
    # historical-membership feed is the proper fix; flag it loudly until then.
    logger.warning(
        "[breadth] computed from CURRENT S&P 500 membership (%d names) across full "
        "history — survivorship bias; early-history breadth reads optimistic "
        "(audit F3, needs date-stamped membership).",
        len(constituents),
    )

    # Full S&P 500 universe — no truncation. The old ``constituents[:100]`` skewed
    # breadth toward alphabetically-early (A–C) names and baked in a fixed count,
    # violating the universe-agnostic rule (NORTH STAR #2). Tickers without at least
    # 200 cached bars are skipped.
    above_by_ticker: dict[str, pd.Series] = {}
    for ticker in constituents:
        try:
            df = cache_load(ticker)
        except (FileNotFoundError, OSError, ValueError) as exc:
            logger.debug("[breadth] skip %s — cache_load failed: %s", ticker, exc)
            continue
        if len(df) < 200:
            continue
        ma200 = df["close"].rolling(200, min_periods=200).mean()
        above_by_ticker[ticker] = (df["close"] > ma200).astype(float)

    if not above_by_ticker:
        return _load_cached_or_empty(parquet_path, staleness_hours)

    # Row-wise % of constituents above their MA200 across the union of dates.
    # Vectorised (replaces the per-date Python loop) so the full universe stays cheap;
    # ``skipna`` drops dates a ticker didn't trade, matching the prior semantics
    # (MA200-warmup bars stay counted as below, exactly as before).
    above_df = pd.DataFrame(above_by_ticker)
    pct = (100.0 * above_df.mean(axis=1, skipna=True)).dropna()
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
            return _load_cached_or_empty(parquet_path, staleness_hours)

    growth = series["XLI"].add(series["XLF"], fill_value=0).dropna()
    defensive = series["XLP"].add(series["XLU"], fill_value=0).dropna()
    common = growth.index.intersection(defensive.index)
    if len(common) < 60:
        return _load_cached_or_empty(parquet_path, staleness_hours)

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


def _load_cached_or_empty(parquet_path: Path,
                          staleness_hours: float = _DEFAULT_STALENESS_HOURS) -> pd.DataFrame:
    """Serve the cached parquet (fail-open) when a fresh read fails, but WARN with
    the cache age when it is past the staleness window — an unbounded-stale cache
    must not masquerade as a fresh feed (mirrors the macro fetchers)."""
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            age = cache_meta.age_seconds(parquet_path)
            if age is not None and age > staleness_hours * 3600:
                logger.warning(
                    "[breadth] serving STALE cache for %s — %.1f h old (> %g h "
                    "window); fresh read failed, value may be outdated.",
                    parquet_path.stem, age / 3600.0, staleness_hours,
                )
            return df
        except (OSError, ValueError) as exc:
            logger.debug("[breadth] cached parquet read failed at %s: %s", parquet_path, exc)
    return pd.DataFrame()
