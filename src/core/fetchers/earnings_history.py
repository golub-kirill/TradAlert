"""
Historical earnings-date fetcher for backtests.

Unlike core.fetchers.earnings_fetcher (which only returns the NEXT
scheduled date), this module returns ALL known earnings dates for a
ticker — past and future — so the backtester can apply the
events.earnings_buffer_days gate correctly at any historical bar.

Storage is delegated to ``core.persistence.json_cache``; this module owns
only the yfinance query logic and the section schema:

    data/fundamentals/{TICKER}.json
        {
            "earnings_history": {
                "dates": ["2019-02-01", "2019-04-30", ..., "2026-08-01"],
                "fetched_at": "2026-05-15T08:00:00"
            },
            ...
        }

ETFs / indices return [] (no earnings exist). Network failures return []
(fail-open — earnings buffer simply does not gate that ticker).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import yfinance as yf

from core.persistence.json_cache import (
    DEFAULT_CACHE_DIR,
    load_fresh_section,
    save_section,
    silence_yfinance,
)
from core.validators.yf_tickerValidator import validate_ticker

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_STALENESS_HOURS: int = 7 * 24  # 1 week; historical dates don't move
_SECTION: str = "earnings_history"


# ── public API ───────────────────────────────────────────────────────────────

def get_earnings_history(
        ticker: str,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        staleness_hours: int = DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> list[date]:
    """
    Return every known earnings date for *ticker*, sorted ascending.

    Past and future dates included. Empty list for ETFs / indices or on
    any fetch failure — backtester treats empty history as "no earnings
    gate for this ticker".

    Parameters
    ----------
    ticker          : Ticker symbol; validated and normalised internally.
    cache_dir       : Root fundamentals cache directory.
    staleness_hours : Cache freshness threshold. Default 1 week.
    force           : When True, bypass cache and always re-fetch.

    Returns
    -------
    list[date]
        Sorted ascending; empty when no earnings exist or fetch failed.
    """
    ticker = validate_ticker(ticker)

    if not force:
        hit, cached = load_fresh_section(
            ticker, _SECTION, staleness_hours, cache_dir,
        )
        if hit:
            dates = _parse_dates(cached.get("dates", []) if cached else [])
            logger.debug(
                "Earnings history cache hit ✓ %s (%d dates)", ticker, len(dates),
            )
            return dates

    logger.debug("Earnings history fetch ↓ %s", ticker)
    dates = _fetch(ticker)
    save_section(
        ticker, _SECTION, {"dates": [d.isoformat() for d in dates]}, cache_dir,
    )
    return dates


def next_earnings_from(history: list[date], asof: date) -> date | None:
    """
    Return the first earnings date in *history* on or after *asof*.

    This is the value FilterEngine.signal() expects in its ``earnings_date``
    parameter — "next scheduled report from this bar's perspective". Returns
    None when *history* is empty or *asof* is past the last known date.

    Parameters
    ----------
    history : Sorted list of earnings dates (output of get_earnings_history).
    asof    : Reference date — the bar's date in a backtest, today() live.

    Returns
    -------
    date | None
    """
    future = [d for d in history if d >= asof]
    return min(future) if future else None


# ── internals ────────────────────────────────────────────────────────────────

def _fetch(ticker: str) -> list[date]:
    """
    Query yfinance for the full earnings_dates DataFrame.

    yfinance returns a DataFrame indexed by Timestamp; every index entry
    is coerced to a python date. Duplicate dates (rare — pre-market vs
    after-hours filings sharing a calendar day) are de-duplicated.
    All exceptions are swallowed and produce an empty list.

    yfinance logs its own ERROR-level "No earnings dates found" message
    for every ETF and index — suppressed via ``silence_yfinance``.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        with silence_yfinance():
            df = yf_ticker.earnings_dates
        if df is None or df.empty:
            return []

        out: list[date] = []
        for ts in df.index:
            try:
                d = ts.date() if hasattr(ts, "date") else None
                if isinstance(d, date):
                    out.append(d)
            except Exception:
                continue
        return sorted(set(out))

    except Exception as exc:
        logger.warning("Earnings history fetch failed for %s — %s", ticker, exc)
        return []


def _parse_dates(raw: list[str]) -> list[date]:
    """
    Coerce a list of ISO date strings (from cache) to python dates.

    Bad entries are skipped with a debug log; the cache reader stays
    fail-open so a single malformed string can't lose the whole history.
    """
    out: list[date] = []
    for s in raw:
        try:
            out.append(date.fromisoformat(s))
        except (TypeError, ValueError):
            logger.debug("Skipping unparseable cached date: %r", s)
    return out
