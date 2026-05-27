"""
Volume-by-Price (VBP) histogram for exit placement.

Used ONLY for exit scoring on held longs. Computes a price-level volume
profile over a lookback window and identifies high-volume nodes that act
as support / resistance.

Public API
----------
compute_vbp(df, lookback, n_bins) -> pd.Series (bin_midpoint → volume_value)
nearest_high_volume_node_above(vbp, price) -> (price, volume) | None
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
    Volume-by-Price histogram over ``lookback`` bars with ``n_bins`` bins.

    Each bar contributes ``close × volume`` to the price bin that contains
    the close. The result is a Series indexed by bin midpoint with the
    aggregated dollar-volume as values.

    Parameters
    ----------
    df : DataFrame with ``close`` and ``volume`` columns.
    lookback : Number of trailing bars to include (default 120).
    n_bins : Number of price bins (default 24).

    Returns
    -------
    pd.Series indexed by bin midpoint (float), values = dollar-volume.
    """
    window = df.tail(lookback)
    if len(window) < 2:
        return pd.Series(dtype=float)

    closes = window["close"].values
    volumes = window["volume"].values
    dollar_vol = closes * volumes

    price_min = closes.min()
    price_max = closes.max()
    if price_max == price_min:
        mid = price_min
        return pd.Series({mid: float(dollar_vol.sum())})

    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_indices = np.digitize(closes, bin_edges[1:-1], right=False)
    bin_indices = np.clip(bin_indices, 0, n_bins - 1)

    bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    agg = np.zeros(n_bins)
    for i in range(len(closes)):
        agg[bin_indices[i]] += dollar_vol[i]

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
