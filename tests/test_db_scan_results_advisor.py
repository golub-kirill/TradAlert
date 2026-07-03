"""advisor_note journaling: row mapping + pre-migration legacy fallback.

Gates the safety fix that keeps a column-less (pre-ALTER) DB from losing the
whole scan journal: on an "Unknown column 'advisor_note'" (errno 1054) the writer
retries the legacy INSERT instead of dropping every row.
"""

from __future__ import annotations

import re

import mysql.connector

from core.types import ScanResult, SignalResult, TickerResult
from persistence import db
from persistence.db import (
    _INSERT_SCAN_RESULT_SQL,
    _INSERT_SCAN_RESULT_SQL_LEGACY,
    _result_to_row,
)


def _fired(ticker="TEST.1", note="") -> TickerResult:
    scan = ScanResult(passed=True, close=100.0, atr=2.0)
    sig = SignalResult(passed=True, direction="long", signal_type="momentum",
                       stop_price=95.0, target_price=110.0)
    sig.advisor_note = note
    return TickerResult(ticker=ticker, scan=scan, signal=sig)


def _cols(sql) -> list[str]:
    m = re.search(r"INSERT INTO scan_results\s*\((.*?)\)", sql, re.S)
    return [c.strip().strip("`") for c in m.group(1).split(",")]


# ── row mapping ──────────────────────────────────────────────────────────────

def test_full_sql_has_advisor_note_column_and_placeholder():
    assert "advisor_note" in _cols(_INSERT_SCAN_RESULT_SQL)
    assert "%(advisor_note)s" in _INSERT_SCAN_RESULT_SQL


def test_legacy_sql_omits_advisor_note():
    assert "advisor_note" not in _cols(_INSERT_SCAN_RESULT_SQL_LEGACY)
    assert "%(advisor_note)s" not in _INSERT_SCAN_RESULT_SQL_LEGACY
    # Legacy is otherwise identical: exactly one column fewer.
    assert len(_cols(_INSERT_SCAN_RESULT_SQL)) - len(_cols(_INSERT_SCAN_RESULT_SQL_LEGACY)) == 1


def test_row_maps_advisor_note():
    row = _result_to_row(1, _fired(note="✅ Agree · 80% — ok"))
    assert row["advisor_note"] == "✅ Agree · 80% — ok"


def test_row_empty_note_is_null():
    assert _result_to_row(1, _fired(note=""))["advisor_note"] is None


def test_row_note_truncated_to_512():
    row = _result_to_row(1, _fired(note="x" * 900))
    assert len(row["advisor_note"]) == 512


# ── save_scan_results legacy fallback ────────────────────────────────────────

class _Cursor:
    def __init__(self, unknown_column: bool):
        self.unknown_column = unknown_column
        self.used_sql = None
        self.rowcount = 0

    def executemany(self, sql, seq):
        if self.unknown_column and sql is _INSERT_SCAN_RESULT_SQL:
            raise mysql.connector.Error(
                msg="Unknown column 'advisor_note' in 'field list'", errno=1054)
        self.used_sql = sql
        self.rowcount = len(list(seq))


class _Conn:
    def __init__(self, cursor):
        self._c = cursor

    def cursor(self):
        return self._c

    def commit(self):
        pass

    def rollback(self):
        pass

    def is_connected(self):
        return True

    def close(self):
        pass


def test_uses_full_sql_when_column_present(monkeypatch):
    cur = _Cursor(unknown_column=False)
    monkeypatch.setattr(db, "_connect", lambda: _Conn(cur))
    n = db.save_scan_results(7, [_fired("TEST.1"), _fired("TEST.2")])
    assert n == 2
    assert cur.used_sql is _INSERT_SCAN_RESULT_SQL


def test_falls_back_to_legacy_on_missing_column(monkeypatch, caplog):
    cur = _Cursor(unknown_column=True)
    monkeypatch.setattr(db, "_connect", lambda: _Conn(cur))
    import logging
    with caplog.at_level(logging.WARNING, logger="persistence.db"):
        n = db.save_scan_results(7, [_fired("TEST.1")])
    assert n == 1  # journal preserved, not lost
    assert cur.used_sql is _INSERT_SCAN_RESULT_SQL_LEGACY
    assert "advisor_note" in caplog.text


def test_other_mysql_errors_still_fail_open(monkeypatch, caplog):
    class _BadCursor(_Cursor):
        def executemany(self, sql, seq):
            raise mysql.connector.Error(msg="deadlock", errno=1213)

    monkeypatch.setattr(db, "_connect", lambda: _Conn(_BadCursor(False)))
    import logging
    with caplog.at_level(logging.ERROR, logger="persistence.db"):
        n = db.save_scan_results(7, [_fired()])
    assert n == 0  # fail-open, never raises
    assert "FAILED" in caplog.text
