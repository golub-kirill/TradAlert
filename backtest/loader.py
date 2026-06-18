"""
One-shot data loader for the backtest sweep engine.

Loads every ticker's OHLCV + indicators exactly once, independent of how
many parameter combinations the sweep will test.  The result is a frozen
snapshot of _TickerPrep objects that each backtester run consumes without
any further I/O.

Public API
----------
    load_universe(...)  -> UniverseData

UniverseData carries:
    prepped     : dict[ticker, _TickerPrep]   — ready for PortfolioBacktester
    market_dfs  : dict[symbol, DataFrame]     — SPY/QQQ regime context
    vix_df      : DataFrame | None            — VIX for volatility axis
    skipped     : dict[ticker, reason]        — tickers excluded + why
    date_range  : (first_bar, last_bar)       — common replay window
    tickers     : list[str]                   — tradeable (non-context) symbols
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pyarrow.parquet as pq

from backtest.backtester import _attach_indicators
from backtest.earnings_history import get_earnings_history
from backtest.portfolio_backtester import _TickerPrep

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

CACHE_DIR = Path("data/prices")
EARNINGS_DIR = Path("data/earnings_history")
MACRO_DIR = Path("data/macro")
BEHAVIORAL_DIR = Path("data/behavioral")
INDICATOR_COLS = ["atr", "rsi", "macd", "macd_signal", "macd_hist"]

# Behavioral parquet files are named by source (sp500_breadth, sector_ratios),
# but classify_behavioral_state reads canonical axis keys (breadth,
# sector_rotation) — the keys live fetch_all_behavioral emits. Map stems to those
# keys; without it breadth/sector silently go missing and default to NEUTRAL.
_BEHAVIORAL_KEY_ALIASES = {
    "sp500_breadth": "breadth",
    "sector_ratios": "sector_rotation",
}

# Symbols treated as market context only — not traded
CONTEXT_SYMBOLS = frozenset({"SPY", "QQQ", "^VIX"})
SECTOR_ETFS = frozenset({
    "XLE", "XLK", "GLD", "XLI", "XLF", "XLP", "XLU", "SMH",
    "XLV", "XLY", "XLB", "XLRE", "XLC",
})


# ── result types ──────────────────────────────────────────────────────────────

class DateRange(NamedTuple):
    first: date
    last: date


@dataclass
class UniverseData:
    """
    Frozen snapshot of all pre-loaded backtest inputs.

    Attributes
    ----------
    prepped         : Per-ticker DataFrames with indicators warm + trimmed,
                      plus earnings history lists.  Ready for PortfolioBacktester.
    market_dfs      : Market-context frames (SPY, QQQ) for regime detection.
    vix_df          : VIX frame for volatility axis; None if unavailable.
    skipped         : Tickers that failed to load, with the failure reason.
    date_range      : (first, last) trading-day dates across all valid tickers.
    tickers         : Tradeable ticker list (context symbols excluded).
    macro_series    : Macro data series for point-in-time MacroState classification.
    behavioral_data : Raw behavioral fetcher output for BehavioralState classification.
    spy_df          : SPY OHLCV for breadth divergence detection.
    """
    prepped: dict[str, _TickerPrep]
    market_dfs: dict[str, pd.DataFrame]
    vix_df: pd.DataFrame | None
    skipped: dict[str, str]
    date_range: DateRange
    tickers: list[str]
    macro_series: dict[str, pd.DataFrame] | None = None
    behavioral_data: dict | None = None
    spy_df: pd.DataFrame | None = None

    @property
    def n_tradeable(self) -> int:
        return len(self.prepped)

    @property
    def total_bars(self) -> int:
        return sum(len(p.df) for p in self.prepped.values())

    def summary(self) -> str:
        dr = self.date_range
        return (
            f"{self.n_tradeable} tickers · "
            f"{dr.first} → {dr.last} · "
            f"{self.total_bars:,} indicator-ready bars · "
            f"{len(self.skipped)} skipped"
        )


# ── public API ────────────────────────────────────────────────────────────────

def _load_behavioral_data(behavioral_dir: Path) -> dict | None:
    """Load ``data/behavioral/`` into the dict ``classify_behavioral_state`` expects.

    Parquet stems are remapped to canonical axis keys via ``_BEHAVIORAL_KEY_ALIASES``
    (e.g. ``sp500_breadth`` → ``breadth``) so the backtest feeds the classifier the
    same key contract as the live ``fetch_all_behavioral``. Subdirectories
    (``form4``, ``short_interest``) are passed through by name. Returns None when the
    directory is absent or empty.
    """
    if not behavioral_dir.exists():
        return None
    data: dict = {}
    for pf in behavioral_dir.glob("*.parquet"):
        key = _BEHAVIORAL_KEY_ALIASES.get(pf.stem, pf.stem)
        try:
            data[key] = pd.read_parquet(pf)
        except Exception as exc:
            logger.warning("[behavioral] failed to load %s: %s", pf.stem, exc)
    for sub in behavioral_dir.iterdir():
        if sub.is_dir():
            data[sub.name] = sub
    if not data:
        return None
    logger.info("[behavioral] loaded %d datasets from cache", len(data))
    return data


def load_universe(
        tickers: list[str],
        ma_slow: int = 200,
        earnings_aware: bool = True,
        cache_dir: Path = CACHE_DIR,
        earnings_dir: Path = EARNINGS_DIR,
        macro_dir: Path = MACRO_DIR,
        behavioral_dir: Path = BEHAVIORAL_DIR,
        start_date: date | None = None,
        end_date: date | None = None,
) -> UniverseData:
    """
    Load, enrich, and warm-up trim all tickers in one pass.

    Steps per ticker
    ----------------
    1. Read parquet with PyArrow (skips corrupted files gracefully).
    2. Attach ATR / RSI / MACD indicators.
    3. Drop leading NaN rows (indicator warm-up).
    4. Require >= ma_slow + 2 bars after warm-up.
    5. Optionally fetch / load cached earnings history.

    Context symbols (SPY, QQQ, ^VIX) are segregated — they feed
    market_dfs / vix_df but are excluded from the tradeable universe.

    Parameters
    ----------
    tickers        : Full watchlist (context symbols included).
    ma_slow        : Minimum-rows guard after warm-up. Default 200.
    earnings_aware : Fetch earnings history per ticker. Default True.
    cache_dir      : OHLCV parquet directory.
    earnings_dir   : Earnings JSON cache directory.
    start_date     : Optional replay window start (trims prepped df).
    end_date       : Optional replay window end.

    Returns
    -------
    UniverseData
    """
    prepped: dict[str, _TickerPrep] = {}
    market_dfs: dict[str, pd.DataFrame] = {}
    vix_df: pd.DataFrame | None = None
    skipped: dict[str, str] = {}

    for ticker in tickers:
        raw = _load_parquet(ticker, cache_dir)
        if raw is None:
            skipped[ticker] = "parquet missing or corrupted"
            logger.warning("skip %-12s — parquet missing or corrupted", ticker)
            continue

        try:
            df = _attach_indicators(raw)
        except Exception as exc:
            skipped[ticker] = f"indicator attach failed: {exc}"
            logger.warning("skip %-12s — indicator attach: %s", ticker, exc)
            continue

        # Warm-up trim — drop rows where any indicator is still NaN
        ready = df[INDICATOR_COLS].notna().all(axis=1)
        if not ready.any():
            skipped[ticker] = "indicators never warm (too few bars)"
            continue
        df = df.loc[ready.idxmax():]

        # Minimum-bars guard
        min_bars = ma_slow + 2
        if len(df) < min_bars:
            skipped[ticker] = f"only {len(df)} bars after warm-up (need {min_bars})"
            continue

        # Optional date window trim
        if start_date:
            df = df.loc[pd.Timestamp(start_date):]
        if end_date:
            df = df.loc[:pd.Timestamp(end_date)]
        if len(df) < min_bars:
            skipped[ticker] = "fewer than min_bars after date-window trim"
            continue

        # ── segregate context vs tradeable ────────────────────────────────
        upper = ticker.upper()

        if upper == "^VIX":
            vix_df = df
            logger.debug("loaded VIX  (%d bars)", len(df))
            continue

        if upper in ("SPY", "QQQ"):
            market_dfs[upper] = df
            logger.debug("loaded %-6s  (%d bars)", upper, len(df))
            continue

        if upper in SECTOR_ETFS:
            market_dfs[upper] = df
            logger.debug("loaded sector %-6s  (%d bars)", upper, len(df))
            continue

        # Tradeable ticker
        eh: list[date] = []
        if earnings_aware:
            try:
                eh = get_earnings_history(ticker, cache_dir=earnings_dir)
            except Exception as exc:
                logger.warning("[%s] earnings history failed — %s", ticker, exc)

        prepped[ticker] = _TickerPrep(df=df, earnings_history=eh)
        logger.debug("loaded %-12s  (%d bars, %d earnings dates)",
                     ticker, len(df), len(eh))

    # ── load macro series from parquet cache ────────────────────────────
    macro_series: dict[str, pd.DataFrame] | None = None
    if macro_dir.exists():
        macro_series = {}
        for pf in macro_dir.glob("*.parquet"):
            sid = pf.stem
            try:
                df_m = pd.read_parquet(pf)
                if "value" in df_m.columns:
                    # Ensure tz-naive index for backtest compatibility
                    if df_m.index.tz is not None:
                        df_m.index = df_m.index.tz_localize(None)
                    macro_series[sid] = df_m
            except Exception as exc:
                logger.warning("[macro] failed to load %s: %s", sid, exc)
        if not macro_series:
            macro_series = None
        else:
            logger.info("[macro] loaded %d series from cache", len(macro_series))

    # ── load behavioral data from parquet cache ─────────────────────────
    behavioral_data = _load_behavioral_data(behavioral_dir)

    # ── segregate SPY for breadth divergence detection ──────────────────
    spy_df = market_dfs.get("SPY", None)

    # ── build date range from tradeable universe ───────────────────────────
    if prepped:
        all_first = [p.df.index[0].date() for p in prepped.values()]
        all_last = [p.df.index[-1].date() for p in prepped.values()]
        dr = DateRange(first=min(all_first), last=max(all_last))
    else:
        today = date.today()
        dr = DateRange(first=today, last=today)

    tradeable = sorted(prepped.keys())
    logger.info(
        "Universe loaded: %d tradeable, %d context, %d skipped | %s → %s",
        len(prepped), len(market_dfs) + (vix_df is not None),
        len(skipped), dr.first, dr.last,
    )
    return UniverseData(
        prepped=prepped,
        market_dfs=market_dfs,
        vix_df=vix_df,
        skipped=skipped,
        date_range=dr,
        tickers=tradeable,
        macro_series=macro_series,
        behavioral_data=behavioral_data,
        spy_df=spy_df,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_parquet(ticker: str, cache_dir: Path) -> pd.DataFrame | None:
    """
    Load one ticker's parquet file, returning None on any failure.

    Tries two strategies:
    1. PyArrow ParquetFile (fast, standard).
    2. Full-file BytesIO read (works around some seek() bugs on network mounts).

    If both fail the file is likely written by a newer pyarrow that added
    page-index data in a format the current reader doesn't support.  Run
    ``python backtest/repair_parquet.py`` from Windows to re-save the cache
    in a universally compatible format.

    Strips timezone from the DatetimeIndex so downstream pandas operations
    stay tz-naive.
    """
    path = cache_dir / f"{ticker.upper()}.parquet"
    if not path.exists():
        return None

    df: pd.DataFrame | None = None

    # Strategy 1: standard path
    try:
        pf = pq.ParquetFile(str(path))
        df = pf.read().to_pandas()
    except Exception as exc1:
        # Strategy 2: slurp into BytesIO (bypasses OS seek on some mounts)
        try:
            import io
            buf = io.BytesIO(path.read_bytes())
            pf = pq.ParquetFile(buf)
            df = pf.read().to_pandas()
        except Exception as exc2:
            logger.warning(
                "%-12s — parquet unreadable (strategy1: %s | strategy2: %s) "
                "→ run 'python backtest/repair_parquet.py' from Windows to fix",
                ticker, exc1, exc2,
            )
            return None

    # One of the two strategies populated df (the failure path returns None
    # above). The assert documents the invariant and narrows the type for the
    # rest of this function.
    assert df is not None
    # Normalise index
    if not isinstance(df.index, pd.DatetimeIndex):
        # Parquet may store the timestamp as a column
        ts_col = next(
            (c for c in df.columns if "time" in c.lower() or "date" in c.lower()),
            None,
        )
        if ts_col:
            df = df.set_index(ts_col)
        df.index = pd.to_datetime(df.index)

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index.name = "timestamp"
    df = df.sort_index().dropna(subset=["close"])

    # Require OHLCV columns
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return None

    return df
