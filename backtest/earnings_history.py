"""
Historical earnings-date fetcher — backward-compat shim.

The implementation lives in ``core.fetchers.earnings_history_store``; this
module re-exports its public names for callers that import from the historical
``backtest.earnings_history`` path. New code should import from the core
location directly.
"""

from __future__ import annotations

from core.fetchers.earnings_history_store import (
    DEFAULT_CACHE_DIR,
    DEFAULT_STALENESS_HOURS,
    get_earnings_history,
    next_earnings_from,
)

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_STALENESS_HOURS",
    "get_earnings_history",
    "next_earnings_from",
]
