"""
Next-earnings-date fetcher for a single ticker.

Returns the next scheduled earnings date, or None for:
    • ETFs, indices, crypto, forex (never report earnings)
    • Equities between cycles (no future date posted yet)
    • Network or parser failures (fail-open, never blocks trading)

Results are cached as JSON for 24h. Cache layout:

    data/earnings_dates/{TICKER}.json
        {
            "ticker":        "AAPL",
            "next_earnings": "2026-05-29",
            "fetched_at":    "2026-05-13T14:23:01"
        }
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from core.validators.yf_tickerValidator import validate_ticker

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _silence_yfinance():
    """
    Raise yfinance's logger to CRITICAL for the duration of the block.

    Suppresses the spurious ERROR-level "No earnings dates found, symbol
    may be delisted" messages that yfinance emits for every ETF and index.
    """
    yf_log = logging.getLogger("yfinance")
    old_level = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        yf_log.setLevel(old_level)


# ── constants ─────────────────────────────────────────────────────────────────

DEFAULT_CACHE_DIR:       Path = Path("data/earnings_dates")
DEFAULT_STALENESS_HOURS: int  = 24


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

    Reads from JSON cache when fresh; otherwise queries yfinance, persists
    the result, and returns it. Network failures are caught and logged.
    A cached `None` value is preserved as a real cache hit.

    Parameters
    ----------
    ticker          : Ticker symbol; validated and normalised internally.
    cache_dir       : Directory for per-ticker JSON files.
    staleness_hours : Cache freshness threshold in hours. Default 24.
    force           : When True, bypass cache and always re-fetch.
    today           : Override for "today" — used by tests.

    Returns
    -------
    date | None
        Next future earnings date, or None when no scheduled date exists
        or the fetch failed.

    Raises
    ------
    FetchError
        Only when the ticker string itself is invalid. Network and parser
        failures are swallowed.
    """
    ticker = validate_ticker(ticker)
    today  = today or date.today()

    if not force:
        hit, cached_date = _load_cache(ticker, cache_dir, staleness_hours)
        if hit:
            logger.debug("Earnings cache hit ✓ %s → %s", ticker, cached_date)
            return cached_date

    logger.debug("Earnings fetch    ↓ %s", ticker)
    next_date = _fetch_from_yfinance(ticker, today)
    _save_cache(ticker, next_date, cache_dir)
    return next_date


# ── internals ─────────────────────────────────────────────────────────────────

def _fetch_from_yfinance(ticker: str, today: date) -> date | None:
    """
    Query yfinance for the next earnings date.

    Tries Ticker.calendar first (lightweight dict). Falls back to
    Ticker.earnings_dates (DataFrame of historical + future dates) when
    calendar is empty or missing 'Earnings Date'. All exceptions return None.
    """
    try:
        yf_ticker = yf.Ticker(ticker)

        # 1. Ticker.calendar — preferred, single dict.
        # ETFs and indices have no fundamentals — yfinance emits HTTP 404 errors
        # for these symbols which are suppressed by _silence_yfinance.
        try:
            with _silence_yfinance():
                calendar = yf_ticker.calendar or {}
            raw_dates = calendar.get("Earnings Date") or []
            future = [
                d for d in (_to_date(x) for x in raw_dates)
                if d is not None and d >= today
            ]
            if future:
                return min(future)
        except Exception as exc:
            logger.debug("calendar lookup failed for %s: %s", ticker, exc)

        # 2. Ticker.earnings_dates — fallback DataFrame indexed by date.
        try:
            with _silence_yfinance():
                ed_df = yf_ticker.earnings_dates
            if ed_df is not None and not ed_df.empty:
                idx_dates = [_to_date(ts) for ts in ed_df.index]
                future = [d for d in idx_dates if d is not None and d >= today]
                if future:
                    return min(future)
        except Exception as exc:
            logger.debug("earnings_dates lookup failed for %s: %s", ticker, exc)

    except Exception as exc:
        logger.warning("Earnings fetch failed for %s — %s", ticker, exc)

    return None


def _to_date(value) -> date | None:
    """
    Coerce a yfinance date-ish value to a python date.

    yfinance returns datetime, date, or pandas Timestamp depending on the
    endpoint and library version. Unrecognised values return None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # pandas Timestamp — duck-typed to avoid importing pandas here.
    if hasattr(value, "date") and callable(value.date):
        try:
            result = value.date()
            return result if isinstance(result, date) else None
        except Exception:
            return None
    return None


# ── cache helpers ─────────────────────────────────────────────────────────────

def _cache_path(ticker: str, cache_dir: Path | str) -> Path:
    return Path(cache_dir) / f"{ticker.upper()}.json"


def _load_cache(
    ticker:          str,
    cache_dir:       Path | str,
    staleness_hours: int,
) -> tuple[bool, date | None]:
    """
    Return (cache_hit, value).

    The two-tuple lets the caller distinguish "cache fresh, answer is None"
    from "cache miss" — None is itself a valid cached value.

    Returns
    -------
    (False, None)         cache file missing, stale, or corrupt
    (True,  None)         cache fresh, no scheduled date
    (True,  date)         cache fresh, scheduled date found
    """
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return False, None

    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=staleness_hours):
        return False, None

    try:
        payload = json.loads(path.read_text())
        raw = payload.get("next_earnings")
        value = date.fromisoformat(raw) if raw else None
        return True, value
    except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
        logger.warning("Corrupt earnings cache for %s — %s", ticker, exc)
        return False, None


def _save_cache(
    ticker:    str,
    next_date: date | None,
    cache_dir: Path | str,
) -> None:
    """
    Persist the next-earnings result as JSON.

    A None result is written explicitly so the cache distinguishes
    "we asked and there's no date" from "we never asked". Write failures
    are logged but never raised — caching is best-effort.
    """
    path = _cache_path(ticker, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker":        ticker.upper(),
        "next_earnings": next_date.isoformat() if next_date else None,
        "fetched_at":    datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        logger.warning("Failed to write earnings cache for %s — %s", ticker, exc)
