"""
Sectioned-JSON cache for per-ticker fundamentals.

Multiple data sources share one file per ticker; each section owns its own
``fetched_at`` timestamp so different staleness windows coexist:

    data/fundamentals/AAPL.json
        {
            "ticker": "AAPL",
            "info":             {..., "fetched_at": "..."},
            "next_earnings":    {..., "fetched_at": "..."},
            "earnings_history": {..., "fetched_at": "..."}
        }

Section writes are read-modify-write. Corrupt files are renamed
``{path}.corrupt`` and treated as a miss.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from core.paths import FUNDAMENTALS_DIR, SETTINGS_YAML
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

DEFAULT_CACHE_DIR: Path = FUNDAMENTALS_DIR
# Absolute path so it resolves regardless of CWD at import time.
_SETTINGS_PATH: Path = SETTINGS_YAML


def staleness_for(section: str, fallback_hours: int) -> int:
    """
    Resolve a section's cache staleness from settings.yaml.

    Reads ``storage.staleness_<section>`` first, falling back to
    ``storage.staleness_hours``, then to ``fallback_hours``.

    Parameters
    ----------
    section        : Section key, e.g. ``"info"`` or ``"earnings_history"``.
    fallback_hours : Value returned when both settings keys are absent.

    Returns
    -------
    int  Staleness threshold in hours.
    """
    if not _SETTINGS_PATH.exists():
        return fallback_hours
    storage = (yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}).get("storage", {})
    section_key = f"staleness_{section}"
    if section_key in storage:
        return int(storage[section_key])
    if "staleness_hours" in storage:
        return int(storage["staleness_hours"])
    return fallback_hours


# ── public API ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def silence_yfinance():
    """
    Raise yfinance's logger to CRITICAL for the duration of the block.

    Yields
    ------
    None
    """
    yf_log = logging.getLogger("yfinance")
    old_level = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        yf_log.setLevel(old_level)


def load_fresh_section(
        ticker: str,
        section: str,
        staleness_hours: int,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
) -> tuple[bool, dict[str, Any] | None]:
    """
    Return (hit, section_data) for one section of a sectioned JSON cache.

    Parameters
    ----------
    ticker          : Ticker symbol (filename uses upper).
    section         : Section key inside the JSON payload, e.g. ``"info"``.
    staleness_hours : Max age of the section's ``fetched_at`` before miss.
    cache_dir       : Root cache directory.

    Returns
    -------
    (False, None)
        File missing, section absent, ``fetched_at`` missing/unparseable,
        section stale, or file corrupt (also quarantined as ``.corrupt``).
    (True, dict)
        Section is fresh; section payload returned without ``fetched_at``.
    """
    path = _cache_path(ticker, cache_dir)
    if not path.exists():
        return False, None

    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Corrupt cache file %s — quarantining: %s", path, exc)
        _quarantine(path)
        return False, None

    section_data = payload.get(section)
    if not isinstance(section_data, dict):
        return False, None

    fetched_at_raw = section_data.get("fetched_at")
    if not isinstance(fetched_at_raw, str):
        return False, None

    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError:
        logger.warning(
            "Bad fetched_at in %s[%s]: %r — treating as miss",
            path, section, fetched_at_raw,
        )
        return False, None

    if datetime.now() - fetched_at > timedelta(hours=staleness_hours):
        return False, None

    # Strip the timestamp before returning — callers want only the data.
    return True, {k: v for k, v in section_data.items() if k != "fetched_at"}


def save_section(
        ticker: str,
        section: str,
        data: dict[str, Any],
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
) -> None:
    """
    Write one section of the sectioned JSON cache; preserve other sections.

    Stamps ``fetched_at = now()`` onto the section payload before writing.

    Parameters
    ----------
    ticker    : Ticker symbol — filename derived as ``{TICKER}.json``.
    section   : Section key, e.g. ``"info"`` or ``"earnings_history"``.
    data      : Section payload. Any caller-supplied ``fetched_at`` is
                overwritten.
    cache_dir : Root cache directory; created if missing.

    Notes
    -----
    Write failures are logged at WARNING and swallowed. The atomic tmp+replace
    keeps a kill mid-write from corrupting the file.

    Concurrency contract: one writer per ticker file at a time, guaranteed by the
    fetch model (one ThreadPoolExecutor task per ticker, keyed by ticker). NOT safe
    against two writers racing the same file — their read-modify-write cycles can
    lose each other's update, so no lock is taken.
    """
    path = _cache_path(ticker, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read-modify-write so writing this section preserves the others. Safe for
    # sequential section writes to one ticker file; the single-writer model (see
    # docstring) rules out overlapping writers.
    if path.exists():
        try:
            payload = json.loads(path.read_text())
            if not isinstance(payload, dict):
                payload = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Corrupt cache %s on save — rebuilding: %s", path, exc,
            )
            payload = {}
    else:
        payload = {}

    payload["ticker"] = ticker.upper()
    payload[section] = {
        **data,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        # Atomic tmp+replace: a kill mid-write would otherwise corrupt the file
        # and quarantine all sections, not just this one.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Failed to write cache %s — %s", path, exc)


# ── internals ─────────────────────────────────────────────────────────────────

def _cache_path(ticker: str, cache_dir: Path | str) -> Path:
    """Resolve the per-ticker JSON file path."""
    return Path(cache_dir) / f"{ticker.upper()}.json"


def _quarantine(path: Path) -> None:
    """Rename a corrupt cache file aside so the next fetch can repopulate."""
    try:
        path.rename(path.with_suffix(path.suffix + ".corrupt"))
    except OSError as exc:
        logger.warning("Could not quarantine %s — %s", path, exc)
