"""Per-ticker news cache — ``data/news/{TICKER}.json``, sectioned by source.

Mirrors ``persistence.json_cache`` (atomic tmp+replace write, corrupt-file
quarantine, per-ticker keying) but lives in its own directory so it never races
the fundamentals cache. Read-modify-write is single-writer-per-ticker safe only;
callers must not fan two writers at the same ticker file.

Layout::

    {"ticker": "AAPL",
     "finnhub": {"headlines": [...], "fetched_at": "2026-07-02T18:00:00+00:00"},
     "search":  {"headlines": [...], "fetched_at": "..."}}
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from core.paths import NEWS_DIR

logger = logging.getLogger(__name__)

__all__ = ["load_fresh_news", "save_news"]

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _cache_path(ticker: str, cache_dir: Path) -> Path:
    """Path-traversal-safe cache file for a ticker."""
    safe = _SAFE.sub("_", ticker).strip(".") or "_"
    return cache_dir / f"{safe}.json"


def _quarantine(path: Path) -> None:
    try:
        path.rename(path.with_suffix(path.suffix + ".corrupt"))
    except OSError:
        pass


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("news cache corrupt %s — quarantining: %s", path.name, exc)
        _quarantine(path)
        return None


def load_fresh_news(
        ticker: str,
        *,
        staleness_hours: float = 4.0,
        cache_dir: Path | str = NEWS_DIR,
) -> list[dict]:
    """Return cached headlines across all sections if fresh, else ``[]``."""
    path = _cache_path(ticker, Path(cache_dir))
    doc = _read(path)
    if not doc:
        return []
    now = datetime.now(timezone.utc)
    merged: list[dict] = []
    for section in ("gathered", "finnhub", "search"):
        blob = doc.get(section) or {}
        fetched = blob.get("fetched_at")
        heads = blob.get("headlines") or []
        if not fetched or not heads:
            continue
        try:
            ts = datetime.fromisoformat(fetched)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h <= staleness_hours:
            merged.extend(heads)
    return merged


def save_news(
        ticker: str,
        section: str,
        headlines: list[dict],
        *,
        cache_dir: Path | str = NEWS_DIR,
) -> None:
    """Write one section's headlines, atomically, stamping ``fetched_at``.

    Read-modify-write preserves the other section. Fail-open (logs on OSError).
    """
    cache_dir = Path(cache_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("news cache mkdir failed %s: %s", cache_dir, exc)
        return
    path = _cache_path(ticker, cache_dir)
    doc = _read(path) or {}
    if not isinstance(doc, dict):
        doc = {}
    doc["ticker"] = ticker
    doc[section] = {
        "headlines": headlines,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("news cache write failed %s: %s", path.name, exc)
