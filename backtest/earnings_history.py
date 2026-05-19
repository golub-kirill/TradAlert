"""
Historical earnings-date fetcher for backtests.

Unlike core.fetchers.earnings_fetcher (which only returns the NEXT
scheduled date), this module returns ALL known earnings dates for a
ticker — past and future — so the backtester can apply the
events.earnings_buffer_days gate correctly at any historical bar.

Cache layout
    data/earnings_history/{TICKER}.json
        {
            "ticker":     "AAPL",
            "dates":      ["2019-02-01", "2019-04-30", ..., "2026-08-01"],
            "fetched_at": "2026-05-15T08:00:00"
        }

ETFs / indices return [] (no earnings exist). Network failures return []
(fail-open — earnings buffer simply does not gate that ticker).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from core.validators.yf_tickerValidator import validate_ticker
from persistence.json_cache import silence_yfinance as _silence_yfinance

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_CACHE_DIR: Path = Path("data/earnings_history")
DEFAULT_STALENESS_HOURS: int = 7 * 24  # 1 week; historical dates don't move


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
    ticker          : Symbol.
    cache_dir       : Directory for per-ticker JSON files.
    staleness_hours : Cache freshness threshold. Default 1 week.
    force           : Bypass cache.

    Returns
    -------
    list[date]
    """
    ticker = validate_ticker(ticker)

    if not force:
        hit, cached = _load_cache(ticker, cache_dir, staleness_hours)
        if hit:
            logger.debug("Earnings history cache hit ✓ %s (%d dates)",
                         ticker, len(cached))
            return cached

    logger.debug("Earnings history fetch ↓ %s", ticker)
    dates = _fetch(ticker)
    _save_cache(ticker, dates, cache_dir)
    return dates


def next_earnings_from(history: list[date], asof: date) -> date | None:
    """
    Return the first earnings date in *history* that is on or after *asof*.

    Re-exported from ``core.fetchers.earnings_history`` — the single canonical
    implementation. Kept here for backward-compatibility with callers that import
    from this module directly (e.g. portfolio_backtester, ticker_store).
    """
    from core.fetchers.earnings_history import next_earnings_from as _canonical
    return _canonical(history, asof)


# ── internals ────────────────────────────────────────────────────────────────

def _fetch(ticker: str) -> list[date]:
    """
    Query yfinance for the full earnings_dates DataFrame.

    yfinance returns a DataFrame indexed by Timestamp; we coerce every
    index entry to a python date. Duplicate dates (rare — pre-market
    vs after-hours filings sharing a calendar day) are de-duplicated.
    All exceptions are swallowed and produce an empty list.

    yfinance logs its own ERROR-level "No earnings dates found" message
    for every ETF and index — suppressed via _silence_yfinance().
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        with _silence_yfinance():
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


def _cache_path(ticker: str, cache_dir: Path | str) -> Path:
    return Path(cache_dir) / f"{ticker.upper()}.json"


def _load_cache(
        ticker: str,
        cache_dir: Path | str,
        staleness_hours: int,
) -> tuple[bool, list[date]]:
    """Return (hit, dates). Corrupt or stale files miss."""
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return False, []

    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=staleness_hours):
        return False, []

    try:
        payload = json.loads(path.read_text())
        dates = [date.fromisoformat(s) for s in payload.get("dates", [])]
        return True, dates
    except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
        logger.warning("Corrupt earnings history cache for %s — %s", ticker, exc)
        return False, []


def _save_cache(ticker: str, dates: list[date], cache_dir: Path | str) -> None:
    path = _cache_path(ticker, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker.upper(),
        "dates": [d.isoformat() for d in dates],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        logger.warning("Failed to write earnings history cache for %s — %s",
                       ticker, exc)
