"""
Cross-layer DTOs (domain types).

This module exists so that ``src/persistence/`` and ``src/core/`` can
share result types without either importing from the application entry
point (``main.py``).

Previously ``TickerResult`` lived in ``main.py`` and was imported by
``persistence.db`` via ``TYPE_CHECKING`` — a layering inversion
(infrastructure → application) that only worked because the cycle was
deferred to type-checking time.

Public types
------------
ScanResult / SignalResult / GateCheck
    Engine result DTOs (moved here from ``core.filter_engine``, which
    re-exports them — this module is a leaf; it must not import the engine).
TickerResult
    Per-ticker outcome of one live scan: scan + optional signal + optional error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "Direction",
    "SignalType",
    "GateCheck",
    "ScanResult",
    "SignalResult",
    "TickerResult",
    "SIGNAL_TYPE",
    "DIRECTION",
    "sign_of",
    "TICKER_TREND",
    "TREND_STATE",
    "VOL_STATE",
]

Direction = Literal["long", "short", "exit_long", "exit_short", "none"]
SignalType = Literal["momentum", "mean_reversion", "regime", "time_stop", "none"]


@dataclass
class ScanResult:
    """
    Output of FilterEngine.scan().

    Attributes
    ----------
    passed       : True when all scan filters cleared.
    reason       : Explanation string; always populated.
    close        : Last-bar close price. None when scan raised before compute.
    atr          : ATR(14) on the last bar.
    atr_pct      : atr / close × 100.
    dv20         : 20-day average dollar volume.
    market_cap   : Market cap in dollars; None when not supplied.
    rsi          : RSI(14) on the last bar.
    macd         : MACD line on the last bar.
    macd_signal  : MACD signal line on the last bar.
    macd_hist    : MACD histogram on the last bar.
    """
    passed: bool
    reason: str = ""

    # ── last-bar snapshot (populated inside scan()) ──────────────────────────
    close: float | None = field(default=None, repr=False)
    atr: float | None = field(default=None, repr=False)
    atr_pct: float | None = field(default=None, repr=False)
    dv20: float | None = field(default=None, repr=False)
    market_cap: float | None = field(default=None, repr=False)
    rsi: float | None = field(default=None, repr=False)
    macd: float | None = field(default=None, repr=False)
    macd_signal: float | None = field(default=None, repr=False)
    macd_hist: float | None = field(default=None, repr=False)


@dataclass
class GateCheck:
    """
    One factor row in the entry-gate "trigger panel".

    A direction-aware, *post-decision* description of a single entry factor —
    the engine's "proof of opinion". Rendered factor-grouped on the chart
    sidebar and folded into the Telegram factor line from the same source, so
    the two surfaces can never disagree with the real decision.

    Building these never affects a decision (they are derived only after a
    signal has fired, behind ``signal(with_checks=True)``), so the backtest and
    sweep paths leave ``checks`` empty and replay bit-identically.

    Attributes
    ----------
    group    : Factor group for layout — "TREND" | "MOMENTUM" | "LOCATION" |
               "VOLATILITY" | "RISK" | "CONTEXT".
    name     : Short row label, e.g. "RSI", "MACD Δ", "R:R".
    passed   : Binary pass/fail; drives ✓/✗ and the per-group summary mark.
    detail   : Value text shown beside the mark, e.g. "62.3", "2.50×".
    strength : Optional grade in [0, 1] for continuous factors → rendered as a
               ●●●○ bar. None marks a hard binary (rendered ✓/✗).
    """
    group: str
    name: str
    passed: bool
    detail: str = ""
    strength: float | None = None


@dataclass
class SignalResult:
    """
    Output of FilterEngine.signal().

    Attributes
    ----------
    passed             : True when a signal fired and all gates cleared.
    direction          : "long" | "short" | "exit_long" | "exit_short" | "none".
    signal_type        : "momentum" | "mean_reversion" | "regime" | "time_stop" | "none".
                         "regime" and "time_stop" pair only with an exit direction
                         ("exit_long" / "exit_short").
    stop_price         : ``close − ATR × atr_multiplier`` on entry. 0.0 on exit.
    target_price       : ``close + risk × min_rr`` on entry. 0.0 on exit.
    min_rr             : Minimum risk:reward ratio from config. 0.0 on exit.
    size_mult          : Position-size multiplier ∈ [0, 1] from macro/behavioral
                         regime. 1.0 = full size, 0.5 = half size, 0.0 = block.
    market_regime      : Regime label at signal time, e.g. ``"BULL_NORMAL"``.
    ticker_trend       : "UPTREND" | "DOWNTREND" | "CHOP" | "N/A".
    reason             : Explanation string; always populated.
    expected_hold_days : (low, high) trading-day range, display-only. The live path
                         (main.py) sets it from the reference backtest's actual
                         bars_held p25-p75 (the single source of truth); this (3, 14)
                         default is the research-consistent fallback if never set.
    """
    passed: bool
    direction: Direction = "none"
    signal_type: SignalType = "none"
    stop_price: float = 0.0
    target_price: float = 0.0
    min_rr: float = 0.0
    size_mult: float = 1.0
    market_regime: str = ""
    ticker_trend: str = ""
    reason: str = ""
    # ── display-only context (set by main.py on the live path) ────────────────
    expected_hold_days: tuple[int, int] = field(default=(3, 14), repr=False)
    # ── entry-gate trigger panel (populated only when signal(with_checks=True)) ──
    checks: list[GateCheck] = field(default_factory=list, repr=False)
    # ── live data-freshness tier (set by main.py's freshness guard, never the engine) ──
    # "LIVE" | "NEEDS_REVIEW". The engine/backtester never touch this → default "LIVE"
    # keeps the backtest byte-identical. A fired entry is downgraded to NEEDS_REVIEW when its
    # data is stale-after-refetch or the overnight gap breaches the ATR threshold.
    tier: str = "LIVE"
    review_reason: str = ""   # e.g. "gap 2.3×ATR · stale 1 session"


# Typo-protected constants for signal types and directions.
# 65+ places in the codebase compare strings like signal.direction == "long"
# or signal.signal_type == "momentum". A Literal type alias catches mypy but
# not runtime typos. Use these constants instead of bare strings.
#
# Backwards-compatible: still stored as plain `str` values, just sourced
# from one place. The string values intentionally match the Literal aliases
# defined above in this module.

class SIGNAL_TYPE:
    MOMENTUM: str = "momentum"
    MEAN_REVERSION: str = "mean_reversion"
    REGIME: str = "regime"
    NONE: str = "none"


class DIRECTION:
    LONG: str = "long"
    SHORT: str = "short"
    EXIT_LONG: str = "exit_long"
    EXIT_SHORT: str = "exit_short"
    NONE: str = "none"


def sign_of(direction: str) -> int:
    """Return +1 for long entries, -1 for short entries.

    Used by ``Trade``, the fill helpers, and the backtesters to collapse
    long/short conditionals into a single sign multiplier. Raises
    ``ValueError`` for any other input so a typo never silently degrades
    to a long trade.
    """
    if direction == DIRECTION.LONG:
        return 1
    if direction == DIRECTION.SHORT:
        return -1
    raise ValueError(
        f"sign_of: direction must be 'long' or 'short', got {direction!r}"
    )


class TICKER_TREND:
    UPTREND: str = "UPTREND"
    DOWNTREND: str = "DOWNTREND"
    CHOP: str = "CHOP"
    NA: str = "N/A"


class TREND_STATE:
    BULL: str = "BULL"
    BEAR: str = "BEAR"
    CHOP: str = "CHOP"


class VOL_STATE:
    LOW: str = "LOW"
    NORMAL: str = "NORMAL"
    HIGH: str = "HIGH"


@dataclass
class TickerResult:
    """
    Per-ticker stage outcomes for one pipeline run.

    Attributes
    ----------
    ticker : Symbol.
    scan   : ScanResult; always present.
    signal : SignalResult, or None when scan failed or signal was skipped.
    error  : Non-empty when an unexpected exception occurred.
    """
    ticker: str
    scan: ScanResult
    signal: SignalResult | None = None
    error: str = ""
