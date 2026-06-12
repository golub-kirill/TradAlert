"""mfe_r/mae_r journaling (backtest/db.py): row mapping + the migration-state
fallback ladder (full → no-excursion → legacy), exercised with a fake
connection so no MySQL is needed.
"""

from datetime import date

import pytest

import backtest.db as db
from backtest.trade import Trade


def _closed_trade() -> Trade:
    t = Trade(
        ticker="TEST.1", signal_type="momentum", direction="long",
        entry_date=date(2024, 1, 1), entry_price=100.0, initial_stop=90.0,
        initial_target=120.0,
    )
    t.update_excursion(112.0, 96.0)
    t.exit_date = date(2024, 1, 8)
    t.exit_price = 105.0
    t.bars_held = 5
    t.r_multiple = t.compute_r()
    t.compute_excursion_r()
    return t


def test_trade_to_row_carries_excursions():
    row = db._trade_to_row(7, _closed_trade())
    assert row["mfe_r"] == pytest.approx(1.2)    # (112-100)/10
    assert row["mae_r"] == pytest.approx(-0.4)   # (96-100)/10
    assert row["run_id"] == 7


class _FakeCursor:
    def __init__(self, columns: set[str]):
        self._columns = columns
        self._probe = None
        self.executed_sql = None
        self.executed_rows = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        assert "information_schema" in sql
        self._probe = params[0]

    def fetchone(self):
        return (1,) if self._probe in self._columns else (0,)

    def executemany(self, sql, rows):
        self.executed_sql = sql
        self.executed_rows = rows
        self.rowcount = len(rows)


class _FakeConn:
    def __init__(self, columns: set[str]):
        self.cur = _FakeCursor(columns)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def is_connected(self):
        return False


def _run_save(monkeypatch, columns: set[str]) -> _FakeCursor:
    conn = _FakeConn(columns)
    monkeypatch.setattr(db, "_connect", lambda: conn)
    n = db.save_backtest_trades(1, [_closed_trade()])
    assert n == 1
    return conn.cur


def test_full_schema_journals_excursions(monkeypatch):
    cur = _run_save(monkeypatch, {"effective_r", "mfe_r"})
    assert "mfe_r" in cur.executed_sql
    assert cur.executed_rows[0]["mfe_r"] == pytest.approx(1.2)


def test_pre_excursion_table_drops_only_excursions(monkeypatch):
    cur = _run_save(monkeypatch, {"effective_r"})
    assert "mfe_r" not in cur.executed_sql
    assert "effective_r" in cur.executed_sql
    assert "mfe_r" not in cur.executed_rows[0]
    assert "effective_r" in cur.executed_rows[0]


def test_legacy_table_drops_all_new_columns(monkeypatch):
    cur = _run_save(monkeypatch, set())
    for key in ("mfe_r", "mae_r", "effective_r", "size_mult", "borrow_annual_rate"):
        assert key not in cur.executed_sql
        assert key not in cur.executed_rows[0]
