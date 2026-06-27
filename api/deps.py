"""Shared helpers: fail-open DB query + config loading.

Every read fails open (logs, returns empty) so a missing DB or cache degrades the
UI to blanks instead of 500s — the same posture the scripts take.
"""

from __future__ import annotations

import logging
import re

import yaml

from api import ROOT

logger = logging.getLogger("api")
CONFIG = ROOT / "config"

# A symbol safe to splice into an argv: must START alphanumeric so it can never be
# read as a CLI flag (e.g. "--out"), then dotted/dashed alphanumerics. Used by the
# backtest run launcher, which passes tickers to the run_backtest subprocess.
# Journal-only paths (e.g. the positions endpoint) validate with the canonical
# core.validators.yf_tickerValidator instead, which also permits '^' index symbols.
TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,15}$")


def query(sql: str, params: tuple | None = None) -> list[dict]:
    """Run a READ query, return rows as dicts. Fail-open: [] on any error.

    Read-only by contract: non-SELECT/WITH statements are refused so this helper
    can never become a mutation path (all writes go through the journal adapter).
    """
    if not sql.lstrip().lower().startswith(("select", "with")):
        logger.error("query() refused a non-read statement")
        return []
    conn = None
    try:
        from persistence.db_conn import connect
        conn = connect()
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as exc:
        logger.warning("query failed: %s", exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def load_yaml(name: str) -> dict:
    try:
        with open(CONFIG / name, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("config read failed for %s: %s", name, exc)
        return {}


def load_company_names() -> dict:
    """ticker -> full company name (warmed by scripts/fetch_company_names.py). Fail-open."""
    import json
    try:
        with open(ROOT / "data" / "company_names.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
