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
TickerResult
    Per-ticker outcome of one live scan: scan + optional signal + optional error.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.filter_engine import ScanResult, SignalResult


# Typo-protected constants for signal types and directions.
# 65+ places in the codebase compare strings like signal.direction == "long"
# or signal.signal_type == "momentum". A Literal type alias catches mypy but
# not runtime typos. Use these constants instead of bare strings.
#
# Backwards-compatible: still stored as plain `str` values, just sourced
# from one place. The string values intentionally match the Literal aliases
# in core.filter_engine.

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
