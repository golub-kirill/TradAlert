"""
Volume-by-Price (VBP) histogram for exit placement.

Used ONLY for exit scoring on held longs. Computes a price-level volume
profile over a lookback window and identifies high-volume nodes that act
as support / resistance.

Public API
----------
compute_vbp(df, lookback, n_bins) -> pd.Series (bin_midpoint → volume_value)
nearest_high_volume_node_above(vbp, price) -> (price, volume) | None
nearest_high_volume_node_below(vbp, price) -> (price, volume) | None
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_vbp(
        df: pd.DataFrame,
        lookback: int = 120,
        n_bins: int = 24,
) -> pd.Series:
    """
    Canonical Volume-by-Price (volume profile) over ``lookback`` bars, ``n_bins`` bins.

    Each bar's volume is distributed across the price bins its ``[low, high]`` range
    spans, in proportion to how much of the range falls in each bin — so volume is
    attributed to where shares actually changed hands, not just to the close. Bins
    span the window's low-to-high price range; a zero-range bar puts all of its
    volume in the single bin holding that price.

    Parameters
    ----------
    df : DataFrame with ``low``, ``high`` and ``volume`` columns.
    lookback : Number of trailing bars to include (default 120).
    n_bins : Number of price bins (default 24).

    Returns
    -------
    pd.Series indexed by bin midpoint (float), values = share-volume.
    """
    window = df.tail(lookback)
    if len(window) < 2:
        return pd.Series(dtype=float)

    lows = window["low"].to_numpy(dtype=float)
    highs = window["high"].to_numpy(dtype=float)
    vols = window["volume"].to_numpy(dtype=float)

    price_min = float(lows.min())
    price_max = float(highs.max())
    if price_max <= price_min:
        return pd.Series({price_min: float(vols.sum())})

    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_lo = bin_edges[:-1]
    bin_hi = bin_edges[1:]
    bin_mids = (bin_lo + bin_hi) / 2.0
    agg = np.zeros(n_bins)

    def _single_bin(price: float) -> int:
        return int(np.clip(
            np.searchsorted(bin_edges, price, side="right") - 1, 0, n_bins - 1))

    for lo, hi, vol in zip(lows, highs, vols):
        if vol <= 0.0:
            continue
        if hi <= lo:
            agg[_single_bin(lo)] += vol
            continue
        # Split the bar's volume across bins by the overlap of [lo, hi] with each.
        overlap = np.clip(np.minimum(hi, bin_hi) - np.maximum(lo, bin_lo), 0.0, None)
        total = overlap.sum()
        if total <= 0.0:
            agg[_single_bin(lo)] += vol
            continue
        agg += vol * (overlap / total)

    return pd.Series(agg, index=bin_mids, name="vbp")


def nearest_high_volume_node_above(
        vbp: pd.Series,
        price: float,
        volume_percentile: int = 70,
) -> tuple[float, float] | None:
    """
    Find the nearest high-volume node *above* the current price.

    Parameters
    ----------
    vbp : Series from ``compute_vbp`` (index = price midpoints).
    price : Current price level.
    volume_percentile : Minimum percentile of volume to qualify as a "node".

    Returns
    -------
    (node_price, node_volume) or None if no qualifying node exists above price.
    """
    if vbp.empty:
        return None

    threshold = np.percentile(vbp.values, volume_percentile)
    above = vbp[vbp.index > price]
    if above.empty:
        return None

    high_vol = above[above >= threshold]
    if high_vol.empty:
        return None

    nearest = high_vol.index.min()
    return float(nearest), float(high_vol.loc[nearest])


def nearest_high_volume_node_below(
        vbp: pd.Series,
        price: float,
        volume_percentile: int = 70,
) -> tuple[float, float] | None:
    """
    Find the nearest high-volume node *below* the current price.

    Mirror of ``nearest_high_volume_node_above``: the support shelf a long can
    fall back on, or the first volume barrier a short must break through on the
    way down. "Clear path below" for a short = no qualifying node nearby.

    Parameters
    ----------
    vbp : Series from ``compute_vbp`` (index = price midpoints).
    price : Current price level.
    volume_percentile : Minimum percentile of volume to qualify as a "node".

    Returns
    -------
    (node_price, node_volume) or None if no qualifying node exists below price.
    """
    if vbp.empty:
        return None

    threshold = np.percentile(vbp.values, volume_percentile)
    below = vbp[vbp.index < price]
    if below.empty:
        return None

    high_vol = below[below >= threshold]
    if high_vol.empty:
        return None

    nearest = high_vol.index.max()
    return float(nearest), float(high_vol.loc[nearest])
