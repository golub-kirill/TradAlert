"""Shared on-disk cache primitives for the disk-backed fetchers.

Each fetcher used to carry an identical mtime-based freshness check and a
``fetched_at`` sidecar writer. This module is the single implementation;
callers pass the max age in seconds (hours×3600 or days×86400).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def is_fresh(path: Path, max_age_seconds: float) -> bool:
    """True iff *path* exists and its mtime is within ``max_age_seconds``."""
    try:
        if not path.exists():
            return False
        age = datetime.now().timestamp() - path.stat().st_mtime
        return age < max_age_seconds
    except (OSError, ValueError) as exc:
        logger.debug("cache freshness check failed for %s: %s", path, exc)
        return False


def age_seconds(path: Path) -> float | None:
    """Seconds since *path* was last modified, or None if it doesn't exist."""
    try:
        if not path.exists():
            return None
        return datetime.now().timestamp() - path.stat().st_mtime
    except (OSError, ValueError) as exc:
        logger.debug("cache age check failed for %s: %s", path, exc)
        return None


def write_meta(meta_path: Path) -> None:
    """Write a ``{"fetched_at": iso}`` sidecar (freshness uses the file mtime)."""
    try:
        meta_path.write_text(
            json.dumps({"fetched_at": datetime.now().isoformat()}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("meta write failed at %s: %s", meta_path, exc)
