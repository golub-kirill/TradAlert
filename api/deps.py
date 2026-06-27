"""Shared helpers: fail-open DB query + config loading.

Every read fails open (logs, returns empty) so a missing DB or cache degrades the
UI to blanks instead of 500s — the same posture the scripts take.
"""

from __future__ import annotations

import logging

import yaml

from api import ROOT

logger = logging.getLogger("api")
CONFIG = ROOT / "config"


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
