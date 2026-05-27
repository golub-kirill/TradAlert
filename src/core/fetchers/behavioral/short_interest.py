"""
Short interest fetcher via yfinance.

Public API
──────────
``fetch_short_interest(ticker) -> dict`` returns::

    {
        "short_percent_of_float": float | None,  # e.g. 0.043 = 4.3%
        "fetched_at": "2026-05-27T18:22:43.123456",
    }

Cache layout
────────────
``data/behavioral/short_interest/<TICKER>.json`` (one file per ticker).
Default staleness is 14 days — short interest is reported bi-weekly by
exchanges, so checking more often is wasted bandwidth.

Failure modes (all return fail-open data, never raise)
──────────────────────────────────────────────────────
- Network failure → load cached JSON, or ``{"short_percent_of_float": None}``.
- yfinance returns no ``shortPercentOfFloat`` field → ``None`` is cached
  so we don't pound the API on every scan.
- Cache file unreadable → log + fall through to live fetch.

Consumed by ``core.scoring._score_short_interest`` and
``core.behavioral._classify_positioning``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from core.paths import BEHAVIORAL_DIR

logger = logging.getLogger(__name__)

_DATA_DIR = BEHAVIORAL_DIR / "short_interest"
_DEFAULT_STALENESS_DAYS = 14


def fetch_short_interest(
        ticker: str,
        data_dir: Path | str | None = None,
        staleness_days: int = _DEFAULT_STALENESS_DAYS,
        force: bool = False,
) -> dict:
    """Return ``{"short_percent_of_float": float | None, "fetched_at": ISO-str}``.

    The function is fail-open: it never raises, only logs and falls back
    to cached data (or the neutral ``{"short_percent_of_float": None}``).
    Scoring layer treats ``None`` as "axis missing" → neutral 0.5 score.
    """
    data_dir = Path(data_dir) if data_dir else _DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / f"{ticker.upper()}.json"

    # ── 1. fresh-cache short-circuit ─────────────────────────────────────
    if not force and _cache_fresh(cache_path, staleness_days):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.debug("[short_int] %s loaded from cache", ticker)
            return data
        except (OSError, ValueError) as exc:
            logger.warning("[short_int] cache read failed for %s: %s",
                           ticker, exc, exc_info=True)
            # Fall through to live fetch.

    # ── 2. live fetch via yfinance ──────────────────────────────────────
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        pct = info.get("shortPercentOfFloat")
        # yfinance returns float in [0, 1] or None. Pass through as-is so
        # the scoring layer's ``si_pct = float(si_pct) * 100`` math works.
        if pct is not None:
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = None
        out = {
            "short_percent_of_float": pct,
            "fetched_at": datetime.now().isoformat(),
        }
        try:
            cache_path.write_text(json.dumps(out), encoding="utf-8")
        except OSError as exc:
            logger.warning("[short_int] cache write failed for %s: %s",
                           ticker, exc, exc_info=True)
        return out
    except (ImportError, AttributeError, KeyError, ValueError,
            TypeError, OSError) as exc:
        # ImportError: yfinance not installed.
        # AttributeError/KeyError/ValueError/TypeError: yfinance API shape
        # changed or returned malformed payload.
        # OSError: network failure on yfinance's underlying request.
        logger.warning("[short_int] fetch failed for %s: %s",
                       ticker, exc, exc_info=True)
        return _load_cached_or_default(cache_path)


# ── helpers ──────────────────────────────────────────────────────────────────


def _cache_fresh(cache_path: Path, staleness_days: int) -> bool:
    """True iff the cache file exists and was written within the window."""
    if not cache_path.exists():
        return False
    try:
        mtime = cache_path.stat().st_mtime
        age_days = (datetime.now().timestamp() - mtime) / 86400
        return age_days < staleness_days
    except (OSError, ValueError) as exc:
        logger.debug("[short_int] freshness check failed for %s: %s",
                     cache_path, exc)
        return False


def _load_cached_or_default(cache_path: Path) -> dict:
    """Load the cached JSON, or return the neutral fallback dict."""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("[short_int] cached JSON unreadable at %s: %s",
                         cache_path, exc)
    return {"short_percent_of_float": None}
