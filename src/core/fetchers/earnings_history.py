"""
Historical earnings-date fetcher.

Returns all known earnings dates for a ticker — past and future — for use by
the backtester and the events.earnings_buffer_days gate.

    data/fundamentals/{TICKER}.json
        {
            "earnings_history": {
                "dates": ["2019-02-01", "2019-04-30", ..., "2026-08-01"],
                "fetched_at": "2026-05-15T08:00:00"
            },
            ...
        }

ETFs / indices and network failures return ``[]``.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from core.fetchers.symbology import to_yf_symbol
from core.validators.yf_tickerValidator import validate_ticker
from persistence.json_cache import (
    DEFAULT_CACHE_DIR,
    load_fresh_section,
    save_section,
    silence_yfinance,
    staleness_for,
)

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

_SECTION: str = "earnings_history"
_FALLBACK_STALENESS_H: int = 7 * 24
DEFAULT_STALENESS_HOURS: int = staleness_for(_SECTION, _FALLBACK_STALENESS_H)


# ── public API ───────────────────────────────────────────────────────────────

def get_earnings_history(
        ticker: str,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        staleness_hours: int = DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> list[date]:
    """
    Return every known earnings date for *ticker*, sorted ascending.

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
    dates = fetch_earnings_dates_from_yfinance(ticker)
    save_section(
        ticker, _SECTION, {"dates": [d.isoformat() for d in dates]}, cache_dir,
    )
    return dates


def fetch_earnings_dates_from_yfinance(ticker: str) -> list[date]:
    """
    Query yfinance ``earnings_dates`` and return a sorted, de-duplicated date list.

    Returns ``[]`` on any failure. yfinance ERROR logs are suppressed via
    ``silence_yfinance``. This is the canonical fetcher used by both the live
    pipeline and the backtest pipeline.
    """
    try:
        import yfinance as yf
        yf_ticker = yf.Ticker(to_yf_symbol(ticker))
        with silence_yfinance():
            df = yf_ticker.earnings_dates
        if df is None or df.empty:
            return []

        out: list[date] = []
        for ts in df.index:
            try:
                # yfinance indexes earnings_dates in the exchange timezone, so
                # ts.date() is the exchange-local calendar date — the correct key
                # for the buffer gate, matching the bar/date comparison side.
                d = ts.date() if hasattr(ts, "date") else None
                if isinstance(d, date):
                    out.append(d)
            except Exception:
                continue
        return sorted(set(out))

    except Exception as exc:
        logger.warning("Earnings history fetch failed for %s — %s", ticker, exc)
        return []


def next_earnings_from(history: list[date], asof: date) -> date | None:
    """
    Return the first earnings date in *history* on or after *asof*.

    This is the single canonical implementation. Both ``backtest.earnings_history``
    and ``core.ticker_store`` import from here — do not duplicate this function.

    Parameters
    ----------
    history : Sorted list of earnings dates.
    asof    : Reference date.

    Returns
    -------
    date | None
    """
    future = [d for d in history if d >= asof]
    return min(future) if future else None


# ── internals ────────────────────────────────────────────────────────────────

def _parse_dates(raw: list[str]) -> list[date]:
    """Coerce ISO date strings from cache to ``date`` objects; skip unparseable entries."""
    out: list[date] = []
    for s in raw:
        try:
            out.append(date.fromisoformat(s))
        except (TypeError, ValueError):
            logger.debug("Skipping unparseable cached date: %r", s)
    return out
