"""stand_down_summary read-path contract (live-journal observability).

Mirrors the fake-cursor idiom in test_db_scan_results_contract.py: no real DB.
Feeds synthetic scan_results rows and asserts the rollup counts, the grouped/
sorted rejection_gates breakdown, and passed_on — plus the fail-open path
(cursor raises → returns None, never raises into the scan).
"""

from __future__ import annotations

import logging

import mysql.connector

from persistence import db

# Row shape returned by the SELECT: (ticker, passed, signal_kind, tier, reason, error)
_ROWS = [
    ("TEST.1", 0, "none", "LIVE", "atr_pct", None),          # blocked: atr_pct gate
    ("TEST.2", 0, "none", "LIVE", "atr_pct", None),          # blocked: atr_pct gate
    ("TEST.3", 0, "none", "LIVE", "price", None),            # blocked: price gate
    ("TEST.4", 1, "entry_long", "LIVE", "entry fired", None),  # fired
    ("TEST.5", 1, "none", "LIVE", "no momentum", None),      # passed scan, no fire
    ("TEST.6", 0, None, "NEEDS_REVIEW", None, "fetch error"),  # error + review + null reason
]


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)


class _RaisingCursor(_FakeCursor):
    def execute(self, sql, params=None):
        raise mysql.connector.Error("connection lost")


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def is_connected(self):
        return True

    def close(self):
        pass


def test_stand_down_summary_aggregates_counts_gates_and_passed_on(monkeypatch):
    cur = _FakeCursor(_ROWS)
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(cur))

    out = db.stand_down_summary(42)

    assert out is not None
    assert out["run_id"] == 42
    assert out["n_scanned"] == 6
    assert out["n_passed_scan"] == 2          # TEST.4, TEST.5
    assert out["n_fired"] == 1                # TEST.4 (signal_kind != none)
    assert out["n_review"] == 1              # TEST.6 NEEDS_REVIEW
    assert out["n_errors"] == 1              # TEST.6 error not null

    # rejection_gates: passed=0 rows grouped by reason, count desc, ties by gate name.
    gates = out["rejection_gates"]
    assert gates[0] == {"gate": "atr_pct", "n": 2}
    rest = {g["gate"]: g["n"] for g in gates[1:]}
    assert rest == {"price": 1, "(unspecified)": 1}  # null reason → placeholder bucket

    # passed_on: passed=1 AND signal_kind none → only TEST.5.
    assert out["passed_on"] == [{"ticker": "TEST.5", "reason": "no momentum"}]


def test_stand_down_summary_is_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(db, "_connect", lambda: _FakeConn(_RaisingCursor(_ROWS)))
    with caplog.at_level(logging.WARNING, logger="persistence.db"):
        out = db.stand_down_summary(7)
    assert out is None  # fail-open: never raises into the scan
    assert "stand_down_summary skipped" in caplog.text
