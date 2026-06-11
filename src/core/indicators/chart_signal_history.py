"""
Signal-history overlay for charts.

Walks the chart's last ``lookback`` bars, calls ``engine.signal`` per bar
(entry mode and exit mode separately), and returns a list of
``HistoricalSignal`` markers for rendering.

Public API
----------
HistoricalSignal — dataclass for one historical marker
collect_signal_history — walk bars and collect signals
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from core.filter_engine import FilterEngine
    from core.position_manager import Position

logger = logging.getLogger(__name__)


@dataclass
class HistoricalSignal:
    """One historical signal marker for chart rendering."""
    bar_date: date
    direction: str  # "long" | "exit_long"
    signal_type: str  # "momentum" | "mean_reversion" | "regime"
    passed: bool
    market_regime: str = ""
    ticker_trend: str = ""
    stop_price: float = 0.0
    target_price: float = 0.0

    @property
    def marker_symbol(self) -> str:
        """Chart marker symbol based on direction."""
        if not self.passed:
            return ""
        if self.direction == "long":
            return "▲"
        if self.direction == "exit_long":
            return "▼"
        return ""


def collect_signal_history(
        ticker: str,
        df: pd.DataFrame,
        engine: FilterEngine,
        market_dfs: dict[str, pd.DataFrame] | None = None,
        vix_df: pd.DataFrame | None = None,
        earnings_history: list[date] | None = None,
        position: Position | None = None,
        lookback: int = 90,
) -> list[HistoricalSignal]:
    """
    Walk the last ``lookback`` bars and collect all signals that fired.

    Parameters
    ----------
    ticker : Symbol.
    df : Full OHLCV DataFrame with indicators attached.
    engine : FilterEngine instance.
    market_dfs : Market index DataFrames for regime.
    vix_df : VIX DataFrame.
    earnings_history : List of upcoming earnings dates.
    position : Open position for this ticker.
    lookback : Number of trailing bars to scan.

    Returns
    -------
    List of HistoricalSignal markers.
    """
    from core.indicators.indicators import attach_indicators

    signals: list[HistoricalSignal] = []
    total_bars = len(df)
    start_idx = max(0, total_bars - lookback)

    # Find next earnings date for each bar
    earnings_dates = sorted(earnings_history) if earnings_history else []

    for i in range(start_idx, total_bars):
        df_slice = df.iloc[:i + 1].copy()

        # Ensure indicators are attached on the slice
        if "rsi" not in df_slice.columns:
            df_slice = attach_indicators(df_slice)

        # Check warmup
        if len(df_slice) < 200:
            continue

        bar_date = df_slice.index[-1].date()

        # Find next earnings after this bar
        next_earnings = None
        for ed in earnings_dates:
            if ed > bar_date:
                next_earnings = ed
                break

        # Entry mode
        try:
            signal = engine.signal(
                ticker, df_slice,
                market_dfs=market_dfs, vix_df=vix_df,
                earnings_date=next_earnings, held_long=False,
            )
        except Exception as exc:
            logger.debug("[signal_history] entry signal failed at %s: %s", bar_date, exc)
            continue

        if signal.passed:
            hs = HistoricalSignal(
                bar_date=bar_date,
                direction=signal.direction,
                signal_type=signal.signal_type,
                passed=signal.passed,
                market_regime=signal.market_regime,
                ticker_trend=signal.ticker_trend,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
            )
            signals.append(hs)

        # Exit mode — only if there's a position or we simulate holding
        if position or i > start_idx + 5:
            try:
                exit_signal = engine.signal(
                    ticker, df_slice,
                    market_dfs=market_dfs, vix_df=vix_df,
                    earnings_date=next_earnings, held_long=True,
                )
            except Exception:
                exit_signal = None

            if exit_signal and exit_signal.passed:
                hs = HistoricalSignal(
                    bar_date=bar_date,
                    direction=exit_signal.direction,
                    signal_type=exit_signal.signal_type,
                    passed=exit_signal.passed,
                    market_regime=exit_signal.market_regime,
                    ticker_trend=exit_signal.ticker_trend,
                )
                signals.append(hs)

    logger.info(
        "[signal_history] %s: %d signals over %d bars",
        ticker, len(signals), lookback,
    )
    return signals
