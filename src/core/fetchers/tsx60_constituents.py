"""
S&P/TSX 60 constituent list from Wikipedia.

Scrapes the current TSX 60 constituents table (~60 large-cap Canadian names)
from the 60-name index page and caches the result for 7 days. Returns ticker
strings with ``.TO`` suffix for Yahoo Finance compatibility.
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

_WIKI_URL = "https://en.wikipedia.org/wiki/S%26P/TSX_60"
_CACHE_FILE = FUNDAMENTALS_DIR / "tsx60_constituents.json"
_CACHE_DAYS = 7


def get_tsx60_constituents(
        cache_path: Path | str | None = None,
        force: bool = False,
) -> list[str]:
    """
    Return the current TSX 60 constituent ticker list.

    Parameters
    ----------
    cache_path : Override cache file location.
    force : Always re-fetch, ignoring cache.

    Returns
    -------
    List of Yahoo Finance-format ticker strings (with ``.TO`` suffix).
    Empty list on failure (fail-open).
    """
    cache_path = Path(cache_path) if cache_path else _CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and _cache_fresh(cache_path):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            tickers = data.get("tickers", [])
            logger.debug("[tsx60] loaded %d tickers from cache", len(tickers))
            return tickers
        except (OSError, ValueError) as exc:
            logger.warning("[tsx60] cache read failed: %s", exc, exc_info=True)

    try:
        resp = requests.get(_WIKI_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TradAlert/1.0",
        })
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("[tsx60] Wikipedia fetch failed: %s", exc, exc_info=True)
        return _load_cached_or_empty(cache_path)

    # Find the constituents table — typically the first table on the page
    tables = soup.find_all("table", class_="wikitable")
    if not tables:
        logger.warning("[tsx60] could not find constituents table on page")
        return _load_cached_or_empty(cache_path)

    # The constituents table usually has "Symbol" or "Ticker" in the header
    target_table = None
    for table in tables:
        header = table.find("th")
        if header and any(word in header.get_text().lower() for word in ["symbol", "ticker"]):
            target_table = table
            break

    if target_table is None:
        target_table = tables[0]

    tickers = []
    for row in target_table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if cells:
            symbol = cells[0].get_text(strip=True)
            # Clean up the symbol
            symbol = symbol.split()[0]  # Take first token
            if not symbol.endswith(".TO"):
                symbol = symbol + ".TO"
            tickers.append(symbol)

    if not tickers:
        logger.error("[tsx60] no tickers parsed from table — HTML schema may have changed")
        return _load_cached_or_empty(cache_path)

    # The S&P/TSX 60 has ~60 members; allow a small band for footnote/drift rows.
    # An out-of-band count means the scrape grabbed the wrong table (e.g. the
    # ~220-name Composite) — reject rather than adopt a wrong universe.
    _MIN_EXPECTED, _MAX_EXPECTED = 55, 70
    if not (_MIN_EXPECTED <= len(tickers) <= _MAX_EXPECTED):
        logger.error(
            "[tsx60] parsed %d tickers (expected %d–%d) — wrong table or Wikipedia "
            "HTML changed; refusing to overwrite cache.",
            len(tickers), _MIN_EXPECTED, _MAX_EXPECTED,
        )
        return _load_cached_or_empty(cache_path)

    _save_cache(cache_path, tickers)
    logger.info("[tsx60] fetched %d constituents", len(tickers))
    return tickers


def _cache_fresh(cache_path: Path) -> bool:
    if not cache_path.exists():
        return False
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(data["fetched_at"])
        return datetime.now() - fetched < timedelta(days=_CACHE_DAYS)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        logger.debug("[tsx60] cache freshness check failed: %s", exc)
        return False


def _save_cache(cache_path: Path, tickers: list[str]) -> None:
    data = {"fetched_at": datetime.now().isoformat(), "tickers": tickers}
    cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_cached_or_empty(cache_path: Path) -> list[str]:
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return data.get("tickers", [])
        except (OSError, ValueError) as exc:
            logger.debug("[tsx60] cache read failed at %s: %s", cache_path, exc)
    return []
