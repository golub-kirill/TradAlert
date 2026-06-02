"""Pytest bootstrap: make ``src/`` importable so the suite runs from the repo root.

The package is not installed (no pyproject.toml); the entry points add ``src``
to ``sys.path`` at runtime. This root conftest does the same for the test
session, so ``pytest tests/`` works from a clean checkout without needing
``PYTHONPATH=src``.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
