"""
S&P 500 constituent list from Wikipedia.

Scrapes the current S&P 500 constituents table from Wikipedia and caches
the result for 7 days. Returns a list of ticker strings (Yahoo Finance
format, e.g. ``"BRK-B"`` for Berkshire Hathaway Class B).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from core.paths import FUNDAMENTALS_DIR

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_CACHE_FILE = FUNDAMENTALS_DIR / "sp500_constituents.json"
_CACHE_DAYS = 7


def get_sp500_constituents(
        cache_path: Path | str | None = None,
        force: bool = False,
) -> list[str]:
    """
    Return the current S&P 500 constituent ticker list.

    Parameters
    ----------
    cache_path : Override cache file location.
    force : Always re-fetch, ignoring cache.

    Returns
    -------
    List of Yahoo Finance-format ticker strings.
    Empty list on failure (fail-open).
    """
    cache_path = Path(cache_path) if cache_path else _CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and _cache_fresh(cache_path):
        try:
            data = json.loads(cache_path.read_text())
            tickers = data.get("tickers", [])
            logger.debug("[sp500] loaded %d tickers from cache", len(tickers))
            return tickers
        except (OSError, ValueError) as exc:
            logger.warning("[sp500] cache read failed: %s", exc, exc_info=True)

    try:
        resp = requests.get(_WIKI_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradAlert/1.0",
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("[sp500] Wikipedia fetch failed: %s", exc, exc_info=True)
        return _load_cached_or_empty(cache_path)

    table = soup.find("table", {"id": "constituents"})
    if table is None:
        logger.warning("[sp500] could not find constituents table on page")
        return _load_cached_or_empty(cache_path)

    tickers = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if cells:
            symbol = cells[0].get_text(strip=True)
            # Wikipedia uses dots for share classes (BRK.B); yfinance uses dashes (BRK-B)
            symbol = symbol.replace(".", "-")
            tickers.append(symbol)

    # structural sanity check on the parsed result. S&P 500 has,
    # by definition, ~500 members. If we see < 480 the Wikipedia HTML
    # almost certainly changed shape; cache from previous good fetch is
    # preferable to writing a corrupt list.
    _MIN_EXPECTED = 480
    if not tickers:
        logger.error("[sp500] no tickers parsed from table — HTML schema may have changed")
        return _load_cached_or_empty(cache_path)
    if len(tickers) < _MIN_EXPECTED:
        logger.error(
            "[sp500] parsed only %d tickers (< %d expected) — Wikipedia HTML "
            "likely changed; refusing to overwrite cache. Inspect manually.",
            len(tickers), _MIN_EXPECTED,
        )
        return _load_cached_or_empty(cache_path)

    _save_cache(cache_path, tickers)
    logger.info("[sp500] fetched %d constituents", len(tickers))
    return tickers


def _cache_fresh(cache_path: Path) -> bool:
    if not cache_path.exists():
        return False
    try:
        data = json.loads(cache_path.read_text())
        fetched = datetime.fromisoformat(data["fetched_at"])
        return datetime.now() - fetched < timedelta(days=_CACHE_DAYS)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        logger.debug("[sp500] cache freshness check failed: %s", exc)
        return False


def _save_cache(cache_path: Path, tickers: list[str]) -> None:
    data = {"fetched_at": datetime.now().isoformat(), "tickers": tickers}
    cache_path.write_text(json.dumps(data, indent=2))


def _load_cached_or_empty(cache_path: Path) -> list[str]:
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            return data.get("tickers", [])
        except (OSError, ValueError) as exc:
            logger.debug("[sp500] cache read failed at %s: %s", cache_path, exc)
    return []
