"""
Historical earnings-date fetcher (standalone-file cache, backtest pipeline).

Persists to ``data/earnings_history/{TICKER}.json``. The live pipeline instead
stores earnings in the sectioned ``data/fundamentals/{TICKER}.json`` cache
(``core.fetchers.earnings_history``).

Both pipelines fetch through the SAME source —
``earnings_history.fetch_earnings_dates_from_yfinance`` — so the two caches cannot
see different date lists for a ticker on the same fetch. Only the on-disk *layout*
differs, kept separate so the backtest can populate its own cache without touching
the live one. The two carry independent ``fetched_at`` stamps and so can differ in
*freshness*, but that is benign: historical earnings dates are stable, and only the
next (future) date moves — which the live pipeline reads from its own fresh cache.

Merging the two layouts into one file is a possible future migration; it is deferred
because it would change the backtest's cache source (a reproducibility shift) for
little benefit now that the content source is already unified.

Cache layout
    data/earnings_history/{TICKER}.json
    {
        "ticker": "AAPL",
        "dates": ["2019-02-01", "2019-04-30", ..., "2026-08-01"],
        "fetched_at": "2026-05-15T08:00:00"
    }

ETFs / indices return [] (no earnings exist). Network failures return []
(fail-open — the earnings buffer simply does not gate that ticker).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from core.paths import EARNINGS_HISTORY_DIR

from core.fetchers.earnings_history import fetch_earnings_dates_from_yfinance
from core.validators.yf_tickerValidator import validate_ticker

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_CACHE_DIR: Path = EARNINGS_HISTORY_DIR
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
    ticker : Symbol.
    cache_dir : Directory for per-ticker JSON files.
    staleness_hours : Cache freshness threshold. Default 1 week.
    force : Bypass cache.

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
    delegate to the single canonical fetcher in
    ``core.fetchers.earnings_history.fetch_earnings_dates_from_yfinance``.

    Both the live pipeline (sectioned-JSON cache under data/fundamentals/) and
    the backtest pipeline (standalone-JSON cache under data/earnings_history/)
    now fetch through the same function, eliminating the dual-yfinance-call
    behaviour where the two caches could see different date lists on the same day.

    Cache *layouts* still differ (tracked separately for future migration);
    cache *content sources* are now unified.
    """
    return fetch_earnings_dates_from_yfinance(ticker)


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
        payload = json.loads(path.read_text(encoding="utf-8"))
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
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write earnings history cache for %s — %s",
                       ticker, exc)
