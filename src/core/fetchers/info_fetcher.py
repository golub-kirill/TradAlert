"""
Market-cap fetcher with 24h JSON cache.

Returns None for tickers without a meaningful market cap (ETFs, indices)
or when yfinance lookup fails. The None signal lets FilterEngine.scan()
skip the market-cap gate cleanly for these symbols.

Cache layout
    data/info/{TICKER}.json
        {
            "ticker":      "AAPL",
            "market_cap":  2900000000000.0,
            "fetched_at":  "2026-05-15T08:00:00"
        }
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

from core.validators.yf_tickerValidator import validate_ticker

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _silence_yfinance():
    """Raise yfinance logger to CRITICAL for the block duration."""
    yf_log = logging.getLogger("yfinance")
    old    = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        yf_log.setLevel(old)


# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_CACHE_DIR:       Path = Path("data/info")
DEFAULT_STALENESS_HOURS: int  = 24


# ── public API ───────────────────────────────────────────────────────────────

def get_market_cap(
    ticker:          str,
    cache_dir:       Path | str  = DEFAULT_CACHE_DIR,
    staleness_hours: int         = DEFAULT_STALENESS_HOURS,
    force:           bool        = False,
) -> float | None:
    """
    Return the latest market cap in dollars, or None.

    Cached values (including cached None for ETFs/indices) are preserved
    until the staleness window elapses. Network and parser failures are
    swallowed — caller stays agnostic to yfinance failure modes.
    """
    ticker = validate_ticker(ticker)

    if not force:
        hit, cached = _load_cache(ticker, cache_dir, staleness_hours)
        if hit:
            logger.debug("Market-cap cache hit ✓ %s → %s", ticker, cached)
            return cached

    logger.debug("Market-cap fetch    ↓ %s", ticker)
    value = _fetch(ticker)
    _save_cache(ticker, value, cache_dir)
    return value


# ── internals ────────────────────────────────────────────────────────────────

def _fetch(ticker: str) -> float | None:
    """
    Query yfinance for market cap.

    Tries fast_info first (lightweight). Falls back to .info on failure.
    Returns None for any unrecoverable lookup error.
    yfinance HTTP 404 errors (ETFs/indices have no fundamentals) are suppressed.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        with _silence_yfinance():
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


def _cache_path(ticker: str, cache_dir: Path | str) -> Path:
    return Path(cache_dir) / f"{ticker.upper()}.json"


def _load_cache(
    ticker:          str,
    cache_dir:       Path | str,
    staleness_hours: int,
) -> tuple[bool, float | None]:
    """
    Return (cache_hit, value). Two-tuple lets the caller distinguish
    "cache fresh with None" from "cache miss".
    """
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return False, None

    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=staleness_hours):
        return False, None

    try:
        payload = json.loads(path.read_text())
        raw     = payload.get("market_cap")
        value   = float(raw) if raw is not None else None
        return True, value
    except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
        logger.warning("Corrupt market-cap cache for %s — %s", ticker, exc)
        return False, None


def _save_cache(
    ticker:    str,
    value:     float | None,
    cache_dir: Path | str,
) -> None:
    path = _cache_path(ticker, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker":     ticker.upper(),
        "market_cap": value,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        logger.warning("Failed to write market-cap cache for %s — %s", ticker, exc)
