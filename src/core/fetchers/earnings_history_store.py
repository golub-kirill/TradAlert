"""
Historical earnings-date fetcher (standalone-file cache, backtest pipeline).

Persists to ``data/earnings_history/{TICKER}.json``. The live pipeline uses a
sectioned ``data/fundamentals/{TICKER}.json`` cache (``core.fetchers.earnings_history``).
Both pipelines fetch through the same source
(``earnings_history.fetch_earnings_dates_from_yfinance``), so only the on-disk
layout differs — kept separate so the backtest populates its own cache without
touching the live one. Independent ``fetched_at`` stamps mean freshness can differ,
which is benign: historical dates are stable and only the next future date moves.

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

import datetime as _dt
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from core.paths import EARNINGS_HISTORY_DIR, EARNINGS_PEAD_DIR

from core.fetchers.earnings_history import fetch_earnings_dates_from_yfinance
from core.pead import EarningsEvent, classify_session
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


def get_earnings_events(
        ticker: str,
        cache_dir: Path | str = EARNINGS_PEAD_DIR,
) -> list[EarningsEvent]:
    """
    Load the per-ticker PEAD earnings cache and return ``EarningsEvent``s.

    Reads ``{cache_dir}/{TICKER}.parquet`` (populated by ``scripts/fetch/pead_fetch.py``),
    mapping each row's ``ann_date`` + ``local_hour`` to an ``EarningsEvent`` with
    its reaction session ('BMO'/'AMC'). Events are returned sorted ascending by date.

    Fail-open: a missing file, a 0-row parquet (ETF / no-earnings ticker), or any
    read/parse error returns ``[]`` — the backtester treats an empty event list as
    "no PEAD gate for this ticker".

    Parameters
    ----------
    ticker : Symbol.
    cache_dir : Directory holding the per-ticker PEAD parquet files.

    Returns
    -------
    list[EarningsEvent]
    """
    ticker = validate_ticker(ticker)
    path = Path(cache_dir) / f"{ticker.upper()}.parquet"

    if not path.exists():
        logger.debug("PEAD earnings cache miss (no file) %s", ticker)
        return []

    try:
        import pandas as pd
        df = pd.read_parquet(path)
        if len(df) == 0:
            logger.debug("PEAD earnings cache empty (0 rows) %s", ticker)
            return []

        events: list[EarningsEvent] = []
        for row in df.itertuples(index=False):
            try:
                ev_date = _dt.date.fromisoformat(str(row.ann_date))
            except ValueError as exc:
                logger.debug("PEAD %s: unparseable ann_date %r — %s",
                             ticker, getattr(row, "ann_date", None), exc)
                continue
            events.append(EarningsEvent(
                date=ev_date,
                session=classify_session(int(row.local_hour)),
            ))

        events.sort(key=lambda e: e.date)
        logger.debug("PEAD earnings cache hit ✓ %s (%d events)",
                     ticker, len(events))
        return events
    except Exception as exc:  # corrupt parquet, missing column, etc. — fail open
        logger.warning("Failed to read PEAD earnings cache for %s — %s",
                       ticker, exc)
        return []


# ── internals ────────────────────────────────────────────────────────────────

def _fetch(ticker: str) -> list[date]:
    """
    Delegate to the canonical fetcher
    ``core.fetchers.earnings_history.fetch_earnings_dates_from_yfinance``,
    shared with the live pipeline so both caches see the same date list.
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
