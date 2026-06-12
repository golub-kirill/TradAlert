"""
Broad-market regime: the MarketRegime state object and its classifier.

Extracted from filter_engine so the most-reused seam in the codebase is a
leaf module: consumers that only need the regime state (backtester, charts,
Telegram, tests) no longer have to import the full engine, and the classifier
is unit-testable as a pure function. ``core.filter_engine`` re-exports every
name here, so ``from core.filter_engine import MarketRegime`` keeps working.

This module must not import ``core.filter_engine`` (it is the dependency
arrow being straightened — tests/test_regime_module.py locks it).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from core.defaults import DEFAULTS

logger = logging.getLogger(__name__)

TrendState = Literal["BULL", "BEAR", "CHOP"]
VolState = Literal["LOW", "NORMAL", "HIGH"]


@dataclass
class MarketRegime:
    """
    Two-axis classification of the broad market.

    Attributes
    ----------
    trend       : "BULL" | "BEAR" | "CHOP".
    volatility  : "LOW" | "NORMAL" | "HIGH". Defaults to NORMAL when vix_df is None.
    vix_rising  : True when VIX has risen over the slope-lookback window. Set
                  by ``classify_market_regime`` from ``regime.vix_slope_lookback_days``
                  (default 5 bars). Consulted by the entry gate when
                  ``regime.vix_slope_block`` is enabled — see the Feb 2025
    macro       : Optional MacroState (default None).
    behavioral  : Optional BehavioralState (default None).
    """
    trend: TrendState
    volatility: VolState
    vix_rising: bool = False
    macro: object | None = None
    behavioral: object | None = None

    @property
    def label(self) -> str:
        """Combined label, e.g. ``"BULL_LOW"``."""
        return f"{self.trend}_{self.volatility}"

    @property
    def allows_longs(self) -> bool:
        """True when trend is BULL and volatility is not HIGH."""
        return self.trend == "BULL" and self.volatility != "HIGH"

    @property
    def allows_shorts(self) -> bool:
        """True when trend is BEAR and volatility is not HIGH.

        Mirror image of ``allows_longs``. The HIGH-volatility carve-out
        applies symmetrically — chaotic markets are bad in both directions.
        """
        return self.trend == "BEAR" and self.volatility != "HIGH"

    @property
    def size_multiplier(self) -> float:
        """Composite position-size multiplier from macro × behavioral.

        Geometric mean preserves the property that if either axis says
        zero-risk, the composite goes to zero. Missing axes contribute 1.0
        (neutral). Result clamped to [0.0, 1.0].
        """
        m = getattr(self.macro, "size_multiplier", 1.0) if self.macro else 1.0
        b = getattr(self.behavioral, "size_multiplier", 1.0) if self.behavioral else 1.0
        try:
            m = float(m);
            b = float(b)
        except (TypeError, ValueError):
            return 1.0
        if m <= 0 or b <= 0:
            return 0.0
        composite = (m * b) ** 0.5  # geometric mean
        return max(0.0, min(1.0, composite))


def classify_market_regime(
        cfg: dict,
        market_dfs: dict[str, pd.DataFrame] | None,
        vix_df: pd.DataFrame | None,
) -> MarketRegime:
    """
    Classify the broad market on trend and volatility.

    Pure function of the filters config and the index/VIX frames —
    ``FilterEngine._market_regime`` delegates here.

    Trend
        ``regime.index_symbols`` (default ``[SPY, QQQ]``) vs each
        ``MA(trend.ma_fast)``. With ``require_all_indices=true``: BULL
        iff all > MA, BEAR iff all < MA, else CHOP. Otherwise majority
        vote. Empty/missing ``market_dfs`` → trend defaults to BULL.

    Volatility
        VIX close vs ``regime.vix_low`` / ``regime.vix_high``. None
        ``vix_df`` → defaults to NORMAL.

    Returns
    -------
    MarketRegime
    """
    rcfg = cfg.get("regime", {})

    # ── volatility ───────────────────────────────────────────────────────
    volatility: VolState
    vix_rising = False
    if vix_df is not None and not vix_df.empty:
        vix_close = float(vix_df["close"].iloc[-1])
        vix_low = rcfg.get("vix_low", DEFAULTS.get("filters.regime.vix_low"))
        vix_high = rcfg.get("vix_high", DEFAULTS.get("filters.regime.vix_high"))
        if vix_close < vix_low:
            volatility = "LOW"
        elif vix_close > vix_high:
            volatility = "HIGH"
        else:
            volatility = "NORMAL"

        # ── slope ──────────────────────────────────────────────────────
        # Compare today's VIX close to the close ``lookback`` bars ago.
        # Set unconditionally; the entry gate decides whether to act on
        # it via ``regime.vix_slope_block``. Defensive on short series.
        lookback = int(rcfg.get("vix_slope_lookback_days", 5))
        if len(vix_df) > lookback:
            vix_ref = float(vix_df["close"].iloc[-1 - lookback])
            vix_rising = vix_close > vix_ref
    else:
        volatility = "NORMAL"

    # ── trend ────────────────────────────────────────────────────────────
    if market_dfs is None or not market_dfs:
        # Default to CHOP (not BULL) when SPY/QQQ caches are missing, so
        # allows_longs == False and the entry gate blocks new entries
        # until data is restored. Logged at ERROR so it's visible in ops.
        logger.error(
            "market_regime: no index data supplied — defaulting to CHOP "
            "to block new entries."
        )
        return MarketRegime(trend="CHOP", volatility=volatility, vix_rising=vix_rising)

    symbols = rcfg.get("index_symbols", ["SPY", "QQQ"])
    require_all = rcfg.get("require_all_indices", True)
    ma_period = cfg["trend"]["ma_fast"]

    votes_up = 0
    votes_dn = 0
    for sym in symbols:
        idx_df = market_dfs.get(sym)
        if idx_df is None or len(idx_df) < ma_period:
            continue
        ma = idx_df["close"].iloc[-ma_period:].mean()
        last = idx_df["close"].iloc[-1]
        if last > ma:
            votes_up += 1
        elif last < ma:
            votes_dn += 1

    total_votes = votes_up + votes_dn
    trend: TrendState
    if total_votes == 0:
        trend = "BULL"
    elif require_all:
        if votes_up == total_votes:
            trend = "BULL"
        elif votes_dn == total_votes:
            trend = "BEAR"
        else:
            trend = "CHOP"
    else:
        if votes_up > votes_dn:
            trend = "BULL"
        elif votes_dn > votes_up:
            trend = "BEAR"
        else:
            trend = "CHOP"

    # Secondary short-term MA alignment gate
    if trend == "BULL":
        ma_short_ok = rcfg.get("require_ma_short_alignment", False)
        if ma_short_ok:
            ma_short = rcfg.get("ma_short", DEFAULTS.get("filters.regime.ma_short"))
            for sym in symbols:
                idx_df = market_dfs.get(sym)
                if idx_df is None or len(idx_df) < ma_short:
                    continue
                ma_s = idx_df["close"].iloc[-ma_short:].mean()
                if idx_df["close"].iloc[-1] < ma_s:
                    trend = "CHOP"
                    break

    return MarketRegime(trend=trend, volatility=volatility, vix_rising=vix_rising)
