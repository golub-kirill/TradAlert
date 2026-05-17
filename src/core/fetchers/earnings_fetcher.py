"""
Next-earnings-date wrapper over the shared earnings-history fetcher.

This module is preserved as a thin wrapper so existing callers
(``main.py: from core.fetchers.earnings_fetcher import get_next_earnings``)
keep working without an import-site change.

Returns the next scheduled earnings date, or None for:
    • ETFs, indices, crypto, forex (never report earnings)
    • Equities between cycles (no future date posted yet)
    • Network or parser failures (fail-open, never blocks trading)
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from core.fetchers.earnings_history import (
    get_earnings_history,
    next_earnings_from,
)
from core.persistence.json_cache import DEFAULT_CACHE_DIR

logger = logging.getLogger(__name__)


# ── constants ─────────────────────────────────────────────────────────────────

DEFAULT_STALENESS_HOURS: int = 24


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

    Thin wrapper: fetches the full earnings history (cached) and selects
    the first date on or after *today*.

    The ``staleness_hours`` argument is passed through to
    ``get_earnings_history`` so a stricter freshness need from this caller
    will force a history refresh; that refresh also satisfies any
    subsequent ``get_earnings_history`` call within its own window.

    Parameters
    ----------
    ticker          : Ticker symbol; validated downstream.
    cache_dir       : Root fundamentals cache directory.
    staleness_hours : Cache freshness threshold in hours. Default 24h —
                      preserved from the legacy API for compatibility.
    force           : When True, bypass cache and always re-fetch.
    today           : Override for "today" — used by tests.

    Returns
    -------
    date | None
        Next future earnings date, or None when no scheduled date exists
        or the underlying fetch failed.
    """
    history = get_earnings_history(
        ticker=ticker,
        cache_dir=cache_dir,
        staleness_hours=staleness_hours,
        force=force,
    )
    return next_earnings_from(history, today or date.today())
