"""latest_scan_run read-path contract (P2 /status dashboard).

latest_scan_run uses ``conn.cursor(dictionary=True).fetchone()``, so this uses a
dict-cursor fake (no real DB). Asserts the row→dict mapping, the empty-table
None, and the fail-open path (cursor raises → None, never into the daemon).
"""

from __future__ import annotations

import logging

import mysql.connector

from persistence import db


class _DictCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _RaisingCursor(_DictCursor):
    def execute(self, sql, params=None):
        raise mysql.connector.Error("connection lost")


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, dictionary=False):
        return self._cursor

    def is_connected(self):
        return True

    def close(self):
        pass


def test_latest_scan_run_maps_row(monkeypatch):
    row = {"id": 9, "created_at": "2026-06-25 18:00:00", "tickers_scanned": 40,
           "scan_passed": 12, "signals_fired": 2, "market_regime": "BULL_NORMAL"}
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(_DictCursor(row)))
    assert db.latest_scan_run() == {
        "run_id": 9, "created_at": "2026-06-25 18:00:00", "tickers_scanned": 40,
        "scan_passed": 12, "signals_fired": 2, "market_regime": "BULL_NORMAL",
    }


def test_latest_scan_run_empty_table_returns_none(monkeypatch):
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(_DictCursor(None)))
    assert db.latest_scan_run() is None


def test_latest_scan_run_is_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(_RaisingCursor(None)))
    with caplog.at_level(logging.WARNING, logger="persistence.db"):
        assert db.latest_scan_run() is None
    assert "latest_scan_run read skipped" in caplog.text
