"""
Live price fetcher with 5-minute JSON cache.

Uses yfinance fast_info.last_price — a single lightweight HTTP call, no
full OHLCV download. Returns None on any failure so the caller degrades
gracefully (current price line is simply omitted from the signal description).

Cache layout
    data/prices_live/{TICKER}.json
        {
            "ticker":      "AAPL",
            "price":       182.45,
            "fetched_at":  "2026-05-15T16:05:00"
        }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

from core.validators.yf_tickerValidator import validate_ticker

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR:      Path = Path("data/prices_live")
DEFAULT_STALENESS_MIN:  int  = 5


# ── public API ────────────────────────────────────────────────────────────────

def get_live_price(
    ticker:          str,
    cache_dir:       Path | str = DEFAULT_CACHE_DIR,
    staleness_min:   int        = DEFAULT_STALENESS_MIN,
    force:           bool       = False,
) -> float | None:
    """
    Return the latest trade price for ticker, or None on any failure.

    Cached for staleness_min minutes. Network and parser failures are
    swallowed — caller treats None as "live price unavailable".

    Parameters
    ----------
    ticker        : Ticker symbol; validated and normalised internally.
    cache_dir     : Directory for per-ticker JSON files.
    staleness_min : Cache freshness threshold in minutes.
    force         : When True, bypass cache and always re-fetch.

    Returns
    -------
    float | None
    """
    ticker = validate_ticker(ticker)

    if not force:
        hit, cached = _load_cache(ticker, cache_dir, staleness_min)
        if hit:
            logger.debug("Live price cache hit ✓ %s → %s", ticker, cached)
            return cached

    logger.debug("Live price fetch ↓ %s", ticker)
    price = _fetch(ticker)
    _save_cache(ticker, price, cache_dir)
    return price


# ── internals ────────────────────────────────────────────────────────────────

def _fetch(ticker: str) -> float | None:
    """Query yfinance fast_info for last_price. Returns None on any error."""
    try:
        fi = yf.Ticker(ticker).fast_info
        price: int = getattr(fi, "last_price", 0)
        if price is not None and price > 0:
            return float(price)
    except Exception as exc:
        logger.debug("Live price fetch failed for %s — %s", ticker, exc)
    return None


def _cache_path(ticker: str, cache_dir: Path | str) -> Path:
    return Path(cache_dir) / f"{ticker.upper()}.json"


def _load_cache(
    ticker:        str,
    cache_dir:     Path | str,
    staleness_min: int,
) -> tuple[bool, float | None]:
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return False, None

    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(minutes=staleness_min):
        return False, None

    try:
        payload = json.loads(path.read_text())
        raw     = payload.get("price")
        value   = float(raw) if raw is not None else None
        return True, value
    except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
        logger.debug("Corrupt live price cache for %s — %s", ticker, exc)
        return False, None


def _save_cache(
    ticker:    str,
    price:     float | None,
    cache_dir: Path | str,
) -> None:
    path = _cache_path(ticker, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker":     ticker.upper(),
        "price":      price,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        logger.debug("Failed to write live price cache for %s — %s", ticker, exc)
