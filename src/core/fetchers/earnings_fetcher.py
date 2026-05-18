"""
Next-earnings-date wrapper over the shared earnings-history fetcher.

Returns the next scheduled earnings date, or None for:
    • ETFs, indices, crypto, forex (never report earnings)
    • Equities between cycles (no future date posted)
    • Network or parser failures
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from core.fetchers.earnings_history import (
    get_earnings_history,
    next_earnings_from,
)
from persistence.json_cache import DEFAULT_CACHE_DIR, staleness_for

logger = logging.getLogger(__name__)


# ── constants ─────────────────────────────────────────────────────────────────

_SECTION: str = "next_earnings"
_FALLBACK_STALENESS_H: int = 24
DEFAULT_STALENESS_HOURS: int = staleness_for(_SECTION, _FALLBACK_STALENESS_H)


# ── public API ────────────────────────────────────────────────────────────────

def get_next_earnings(
    ticker:          str,
    cache_dir:       Path | str  = DEFAULT_CACHE_DIR,
    staleness_hours: int         = DEFAULT_STALENESS_HOURS,
    force:           bool        = False,
    today:           date | None = None,
) -> date | None:
    """
    Return the next scheduled earnings date for *ticker*, or None.

    Fetches the full earnings history (cached) and selects the first date on
    or after *today*. ``staleness_hours`` propagates to ``get_earnings_history``.

    Parameters
    ----------
    ticker          : Ticker symbol; validated downstream.
    cache_dir       : Root fundamentals cache directory.
    staleness_hours : Cache freshness threshold in hours.
    force           : When True, bypass cache and always re-fetch.
    today           : Override for "today" — used by tests.

    Returns
    -------
    date | None
        Next future earnings date, or None when none exists or the fetch failed.
    """
    history = get_earnings_history(
        ticker=ticker,
        cache_dir=cache_dir,
        staleness_hours=staleness_hours,
        force=force,
    )
    return next_earnings_from(history, today or date.today())
