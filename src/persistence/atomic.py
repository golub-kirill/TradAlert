"""Crash-safe file writes — temp file in the same directory, then ``os.replace``.

``Path.write_text`` truncates the target and then writes. A process killed
between those two steps leaves a truncated or empty file, and every reader in
this repo treats an unparseable cache as absent (``except Exception: return {}``)
— so a mid-write crash silently discards state rather than failing loudly. Two
places that actually costs something:

* ``scripts/live/intraday_monitor.py`` — the dedup state file. Lose it and every
  armed alert re-arms, so the operator gets duplicate alerts.
* ``src/core/advisor/news_fetcher.py`` — the AlphaVantage daily budget counter.
  Lose it and the count resets to 0 mid-day, so the free 25/day cap is exceeded.

``os.replace`` is atomic on POSIX and on Windows (same volume), which is why the
temp file must be a sibling of the target rather than in a temp directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _tmp_for(path: Path) -> Path:
    """Sibling temp path, unique per process.

    Per-process because the scanner, the intraday monitor and the Telegram daemon
    can all be live at once and touch the same caches; a shared ``.tmp`` name
    would let one process's replace consume another's half-written file.
    """
    return path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")


def write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically, creating parent directories.

    Raises whatever the underlying IO raises — callers own their own fail-open
    policy (most log and continue). A failed write leaves the previous file
    intact, never a truncated one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        # Don't leave the sibling behind on failure — including on KeyboardInterrupt,
        # which is a plausible way to kill a long scan.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_json(path: Path | str, obj: Any, *, indent: int | None = None,
               encoding: str = "utf-8") -> None:
    """``json.dumps`` + :func:`write_text`. Serialisation happens BEFORE the file
    is touched, so an unserialisable object cannot destroy the existing cache."""
    write_text(path, json.dumps(obj, indent=indent), encoding=encoding)
