"""
Percentile-Rank Relative Strength (RP) — cross-sectional ranking.

Computes IBD-style weighted return for every ticker in a combined
universe (S&P 500 + TSX 60) and returns a percentile rank in [0, 99].

Weighted return formula:
 RP = 0.4 × R₃ₘ + 0.2 × R₆ₘ + 0.2 × R₉ₘ + 0.2 × R₁₂ₘ

where Rₙₘ = (close_now / close_n_months_ago) − 1

Public API
----------
compute_rp_weighted_return(df) -> float
build_rp_rank_table(universe_dfs, as_of) -> dict[str, float]
build_rp_rank_matrix(universe_dfs) -> pd.DataFrame
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Approximate trading days per month
_TRADING_DAYS_PER_MONTH = 21

# Weights per IBD methodology
_WEIGHTS = {3: 0.4, 6: 0.2, 9: 0.2, 12: 0.2}


def compute_rp_weighted_return(df: pd.DataFrame) -> float:
    """
    Compute the IBD-style weighted return for a single ticker.

    Parameters
    ----------
    df : DataFrame with ``close`` column and DatetimeIndex.

    Returns
    -------
    Weighted return as a float. NaN if insufficient data.
    """
    if len(df) < 252:  # Need ~12 months of data
        return float("nan")

    close = df["close"]
    weighted = 0.0
    for months, weight in _WEIGHTS.items():
        bars = months * _TRADING_DAYS_PER_MONTH
        if len(close) > bars:
            ret = close.iloc[-1] / close.iloc[-bars - 1] - 1.0
            weighted += weight * ret
        else:
            return float("nan")

    return float(weighted)


def build_rp_rank_table(
        universe_dfs: dict[str, pd.DataFrame],
        as_of: date | None = None,
) -> dict[str, float]:
    """
    Build a percentile-rank table for all tickers in the universe.

    Parameters
    ----------
    universe_dfs : Ticker → OHLCV DataFrame (full history).
    as_of : Slice each DataFrame to this date for point-in-time
    correctness. None → use all available data.

    Returns
    -------
    dict mapping ticker to percentile rank in [0, 99].
    Tickers with insufficient data are excluded.
    """
    if as_of is not None:
        as_of_ts = pd.Timestamp(as_of)

    returns: dict[str, float] = {}
    for ticker, df in universe_dfs.items():
        if df is None or df.empty:
            continue
        df_slice = df.loc[:as_of_ts] if as_of is not None else df
        rp = compute_rp_weighted_return(df_slice)
        if not np.isnan(rp):
            returns[ticker] = rp

    if not returns:
        return {}

    # Percentile rank: 0 = worst, 99 = best
    series = pd.Series(returns)
    ranks = series.rank(pct=True)
    # Scale to [0, 99]
    rank_table = {ticker: int(round(r * 99)) for ticker, r in ranks.items()}

    logger.info(
        "[rp_rank] built rank table for %d tickers (as_of=%s)",
        len(rank_table), as_of,
    )
    return rank_table


def build_rp_rank_matrix(
        universe_dfs: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Percentile rank for EVERY ticker on EVERY date, as a (dates x tickers) frame.

    Same factor and same ranking as ``build_rp_rank_table``, computed for the whole
    history at once. That function re-slices and re-ranks per call, which is fine
    for one live scan but unusable per-bar across a backtest (207 names x ~6600
    bars); this is the vectorised form.

    Each row is ranked independently across the tickers that have a value on that
    date, so the cross-section grows as names warm up. Values are percentile ranks
    in [0, 99]; NaN where the ticker lacks the ~12 months of history the factor
    needs, and rows before any ticker warms up are all-NaN.

    Point-in-time by construction: row ``t`` uses only closes up to and including
    ``t``. A caller acting on the signal must still read the row STRICTLY BEFORE
    the bar it trades on, exactly as the engine does.
    """
    weighted = {}
    for ticker, df in universe_dfs.items():
        if df is None or df.empty or "close" not in df:
            continue
        close = df["close"]
        # Mirrors compute_rp_weighted_return: R_n = close_t / close_{t-n*21} - 1.
        # shift() yields NaN for the warmup span, which is the vector equivalent of
        # that function's "insufficient data -> NaN" guard.
        total = None
        for months, weight in _WEIGHTS.items():
            ret = close / close.shift(months * _TRADING_DAYS_PER_MONTH) - 1.0
            term = weight * ret
            total = term if total is None else total + term
        weighted[ticker] = total

    if not weighted:
        return pd.DataFrame()

    frame = pd.DataFrame(weighted).sort_index()
    ranks = frame.rank(axis=1, pct=True) * 99.0
    logger.info(
        "[rp_rank] built rank matrix: %d dates x %d tickers",
        len(ranks), ranks.shape[1],
    )
    return ranks
