"""
Market-cap fetcher backed by the shared sectioned-JSON fundamentals cache.

Returns None for tickers without a meaningful market cap (ETFs, indices)
or when yfinance lookup fails. The None signal lets FilterEngine.scan()
skip the market-cap gate cleanly for these symbols.

Storage is delegated to ``core.persistence.json_cache``; this module owns
only the yfinance query logic and the section schema:

    data/fundamentals/{TICKER}.json
        {
            "info": {
                "market_cap": 2900000000000.0,
                "fetched_at": "2026-05-15T08:00:00"
            },
            ...
        }
"""

from __future__ import annotations

import logging
from pathlib import Path

import yfinance as yf

from core.validators.yf_tickerValidator import validate_ticker
from persistence.json_cache import (
    DEFAULT_CACHE_DIR,
    load_fresh_section,
    save_section,
    silence_yfinance,
)

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

DEFAULT_STALENESS_HOURS: int = 24
_SECTION: str = "info"


# ── public API ────────────────────────────────────────────────────────────────

def get_market_cap(
    ticker:          str,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        staleness_hours: int = DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> float | None:
    """
    Return the latest market cap in dollars, or None.

    Cached values (including cached None for ETFs/indices) are preserved
    until the staleness window elapses. Network and parser failures are
    swallowed — caller stays agnostic to yfinance failure modes.

    Parameters
    ----------
    ticker          : Ticker symbol; validated and normalised internally.
    cache_dir       : Root fundamentals cache directory.
    staleness_hours : Cache freshness threshold. Default 24h.
    force           : When True, bypass cache and always re-fetch.

    Returns
    -------
    float | None
        Market cap in dollars, or None for symbols without fundamentals.
    """
    ticker = validate_ticker(ticker)

    if not force:
        hit, cached = load_fresh_section(
            ticker, _SECTION, staleness_hours, cache_dir,
        )
        if hit:
            value = cached.get("market_cap") if cached else None
            logger.debug("Market-cap cache hit ✓ %s → %s", ticker, value)
            return value

    logger.debug("Market-cap fetch    ↓ %s", ticker)
    value = _fetch(ticker)
    save_section(ticker, _SECTION, {"market_cap": value}, cache_dir)
    return value


# ── internals ─────────────────────────────────────────────────────────────────

def _fetch(ticker: str) -> float | None:
    """
    Query yfinance for market cap.

    Tries ``fast_info`` first (lightweight). Falls back to ``.info`` on
    failure. Returns None for any unrecoverable lookup error — yfinance
    HTTP 404s (ETFs/indices have no fundamentals) are suppressed by
    ``silence_yfinance``.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        with silence_yfinance():
            try:
                fi = yf_ticker.fast_info
                mc = getattr(fi, "market_cap", None)
                if mc is not None and mc > 0:
                    return float(mc)
            except Exception as exc:
                logger.debug("fast_info failed for %s: %s", ticker, exc)

            try:
                info = yf_ticker.info or {}
                mc   = info.get("marketCap")
                if mc is not None and mc > 0:
                    return float(mc)
            except Exception as exc:
                logger.debug(".info lookup failed for %s: %s", ticker, exc)

    except Exception as exc:
        logger.warning("Market-cap fetch failed for %s — %s", ticker, exc)

    return None