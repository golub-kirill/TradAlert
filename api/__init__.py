"""TradAlert control API — thin FastAPI layer over the existing engine + journal.

Importing this package puts the repo root and ``src/`` on ``sys.path`` (so
``core`` / ``persistence`` / ``backtest`` import) and loads ``config/secrets.env``,
mirroring how the scripts bootstrap. Run with:  uvicorn api.main:app --reload
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / "config" / "secrets.env")
except Exception:
    pass
