"""
Project-root anchored filesystem paths.

Previously many fetcher modules carried their own relative ``Path("data/…")``
constants. Those resolve against the current working directory — running
``python /path/to/main.py`` from a different shell location creates a
second ``data/`` tree in the wrong place. Same for ``config/``.

Anchor everything off this file's location instead. All callers import
from here; relative defaults become absolute and CWD-independent.
"""

from __future__ import annotations

from pathlib import Path

# This file is at .../src/core/paths.py — repo root is three parents up.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent

CONFIG_DIR: Path = PROJECT_ROOT / "config"
DATA_DIR: Path = PROJECT_ROOT / "data"

PRICES_DIR: Path = DATA_DIR / "prices"
PRICES_LIVE_DIR: Path = DATA_DIR / "prices_live"
FUNDAMENTALS_DIR: Path = DATA_DIR / "fundamentals"
EARNINGS_HISTORY_DIR: Path = DATA_DIR / "earnings_history"
BEHAVIORAL_DIR: Path = DATA_DIR / "behavioral"
MACRO_DIR: Path = DATA_DIR / "macro"
SCREENSHOTS_DIR: Path = DATA_DIR / "screenshots"

SETTINGS_YAML: Path = CONFIG_DIR / "settings.yaml"
FILTERS_YAML: Path = CONFIG_DIR / "filters.yaml"
WATCHLIST_YAML: Path = CONFIG_DIR / "watchlist.yaml"
