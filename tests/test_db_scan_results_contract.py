"""scan_results write contract for the live-journal path (audit H4, C1 regression).

Locks three things that together catch a tier/review_reason schema drift (C1):
  • the INSERT's column list, its %(name)s placeholders, and the dict _result_to_row
    builds stay mutually consistent (no drift between SQL and code);
  • every column the INSERT writes exists in the fresh schema (scan_schema.sql), which
    defines BOTH tier and review_reason (the one-off upgrade .sql is owner-applied and
    not kept in the repo);
  • save_scan_results returns the rowcount on success and fails loud-but-open
    (logs ERROR, returns 0, never raises) when the INSERT errors.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import mysql.connector

from core.types import ScanResult, SignalResult, TickerResult
from persistence import db
from persistence.db import _INSERT_SCAN_RESULT_SQL, _result_to_row

_ROOT = Path(__file__).resolve().parent.parent


def _fired_long(ticker: str = "TEST.1", tier: str = "LIVE", review_reason: str = "") -> TickerResult:
    scan = ScanResult(passed=True, close=100.0, atr=2.0)
    sig = SignalResult(passed=True, direction="long", signal_type="momentum",
                       stop_price=95.0, target_price=110.0,
                       tier=tier, review_reason=review_reason)
    return TickerResult(ticker=ticker, scan=scan, signal=sig)


def _insert_columns() -> list[str]:
    m = re.search(r"INSERT INTO scan_results\s*\((.*?)\)", _INSERT_SCAN_RESULT_SQL, re.S)
    return [c.strip().strip("`") for c in m.group(1).split(",")]


def _insert_placeholders() -> list[str]:
    return re.findall(r"%\((\w+)\)s", _INSERT_SCAN_RESULT_SQL)


def _create_block_columns(sql_text: str, table: str) -> set[str]:
    m = re.search(rf"CREATE TABLE[^(]*\b{re.escape(table)}\b\s*\((.*?)\n\)\s*ENGINE",
                  sql_text, re.S | re.I)
    assert m, f"{table} CREATE block not found"
    cols: set[str] = set()
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        first = line.split()[0].strip("`")
        if first.upper() in {"PRIMARY", "KEY", "CONSTRAINT", "FOREIGN", "UNIQUE", "INDEX"}:
            continue
        cols.add(first)
    return cols


# ── SQL ↔ placeholder ↔ dict-key contract ────────────────────────────────────

def test_insert_columns_match_placeholders_in_order():
    assert _insert_columns() == _insert_placeholders()


def test_every_placeholder_has_exactly_one_row_key():
    row = _result_to_row(1, _fired_long())
    assert set(_insert_placeholders()) == set(row.keys())


# ── schema consistency (the C1-catching layer) ───────────────────────────────

def test_insert_columns_all_exist_in_fresh_schema():
    schema = (_ROOT / "data" / "scan_schema.sql").read_text(encoding="utf-8")
    cols = _create_block_columns(schema, "scan_results")
    missing = set(_insert_columns()) - cols
    assert not missing, f"INSERT writes columns absent from scan_schema.sql: {missing}"


def test_fresh_schema_defines_tier_and_review_reason():
    schema = (_ROOT / "data" / "scan_schema.sql").read_text(encoding="utf-8")
    cols = _create_block_columns(schema, "scan_results")
    assert {"tier", "review_reason"} <= cols


def test_fresh_schema_defines_declined():
    # The Telegram 🚫 Skip button sets scan_results.declined via db.mark_declined;
    # it is UPDATE-only (defaults 0), so it isn't in the INSERT but must exist.
    schema = (_ROOT / "data" / "scan_schema.sql").read_text(encoding="utf-8")
    cols = _create_block_columns(schema, "scan_results")
    assert "declined" in cols
    assert "declined" not in set(_insert_columns())   # not written by the INSERT


# ── save_scan_results behaviour (fake cursor; no DB) ─────────────────────────

class _FakeCursor:
    def __init__(self):
        self.executed: list = []
        self.rowcount = 0

    def execute(self, *a, **k):
        pass

    def executemany(self, sql, seq):
        rows = list(seq)
        self.executed.append((sql, rows))
        self.rowcount = len(rows)


class _RaisingCursor(_FakeCursor):
    def executemany(self, sql, seq):
        raise mysql.connector.Error("Unknown column 'tier' in 'field list'")


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def is_connected(self):
        return True

    def close(self):
        pass


def test_save_scan_results_executes_contract_and_returns_rowcount(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(cur))
    results = [_fired_long("TEST.1"), _fired_long("TEST.2")]
    n = db.save_scan_results(7, results)
    assert n == 2
    sql, rows = cur.executed[0]
    assert sql is _INSERT_SCAN_RESULT_SQL
    assert len(rows) == 2
    placeholders = set(_insert_placeholders())
    for r in rows:
        assert set(r.keys()) == placeholders


def test_save_scan_results_failure_is_loud_and_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(_RaisingCursor()))
    with caplog.at_level(logging.ERROR, logger="persistence.db"):
        n = db.save_scan_results(7, [_fired_long()])
    assert n == 0  # fail-open: never raises into the scan
    assert "FAILED" in caplog.text and "INCOMPLETE" in caplog.text
