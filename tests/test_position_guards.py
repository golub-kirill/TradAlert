"""Open-position guards: risk geometry, validate_open rejections, budget advisory.

Pure (no DB): exercises position_manager.risk_unit / validate_open / open_risk_advisory
directly with explicit inputs, so the open path can't journal an invalid position.
"""

from __future__ import annotations

import pytest

from core import position_manager as pm
from exceptions import ValidationError


# ── risk geometry (the single source, shared with reconcile_fills) ───────────

def test_risk_unit_sign_by_side():
    assert pm.risk_unit("long", 100.0, 90.0) == 10.0     # stop below entry → +risk
    assert pm.risk_unit("long", 100.0, 110.0) == -10.0   # stop above → inverted
    assert pm.risk_unit("short", 100.0, 110.0) == 10.0   # stop above entry → +risk
    assert pm.risk_unit("short", 100.0, 90.0) == -10.0   # stop below → inverted


# ── validate_open: accepts valid opens ──────────────────────────────────────

def test_validate_open_accepts_valid():
    pm.validate_open("NVDA", 142.55, "long", 134.0, open_tickers=set())   # long, stop below
    pm.validate_open("HMM", 10.0, "short", 11.5, open_tickers=set())      # short, stop above
    pm.validate_open("AAPL", 200.0, "long", None, open_tickers=set())     # missing stop allowed


# ── validate_open: hard rejections ──────────────────────────────────────────

@pytest.mark.parametrize("ticker", ["TEST", "TEST.1", "test.2", "Test.TO"])
def test_rejects_test_tickers(ticker):
    with pytest.raises(ValidationError):
        pm.validate_open(ticker, 100.0, "long", 90.0, open_tickers=set())


def test_rejects_inverted_stop_long_and_short():
    # the XYZ bug: long with the stop above entry → non-positive risk unit
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 88.40, "long", 93.10, open_tickers=set())
    # short with the stop below entry
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 88.40, "short", 80.0, open_tickers=set())


def test_rejects_bad_price_side_and_stop():
    for entry in (0.0, -5.0, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            pm.validate_open("AB", entry, "long", None, open_tickers=set())
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 100.0, "sideways", 90.0, open_tickers=set())
    with pytest.raises(ValidationError):
        pm.validate_open("AB", 100.0, "long", 0.0, open_tickers=set())   # stop ≤ 0


def test_rejects_duplicate_open_case_insensitive():
    with pytest.raises(ValidationError):
        pm.validate_open("NVDA", 142.55, "long", 134.0, open_tickers={"NVDA"})
    with pytest.raises(ValidationError):
        pm.validate_open("nvda", 142.55, "long", 134.0, open_tickers={"NVDA"})


def test_rejection_carries_detail_and_ticker():
    with pytest.raises(ValidationError) as ei:
        pm.validate_open("AB", 88.40, "long", 93.10, open_tickers=set())
    assert ei.value.ticker == "AB"
    assert "below entry" in ei.value.detail


# ── budget advisory ─────────────────────────────────────────────────────────

def test_open_risk_advisory():
    assert pm.open_risk_advisory(5.0, open_count=2) is None       # within budget
    assert pm.open_risk_advisory(5.0, open_count=5) is not None   # at the cap
    assert pm.open_risk_advisory(5.0, open_count=6) is not None   # over
    assert pm.open_risk_advisory(None, open_count=99) is None     # no cap configured
    assert pm.open_risk_advisory(0, open_count=99) is None


# ── partial scale-out guards (add_partial) ──────────────────────────────────

from datetime import date  # noqa: E402


class _PartialCur:
    """Serves the position row + existing partials for add_partial, records INSERTs."""
    def __init__(self, pos_row, partial_rows):
        self._pos_row = pos_row
        self._partial_rows = partial_rows
        self.lastrowid = 99
        self.inserted = None

    def execute(self, sql, params=None):
        if sql is pm._INSERT_PARTIAL_SQL:
            self.inserted = params

    def fetchone(self):
        return self._pos_row

    def fetchall(self):
        return self._partial_rows


class _PartialConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, dictionary=False):
        return self._cur

    def commit(self):
        pass

    def is_connected(self):
        return True

    def close(self):
        pass


def test_add_partial_rejects_bad_fraction():
    # fraction range is checked BEFORE any DB call → pure
    for f in (0.0, -0.1, 1.5):
        with pytest.raises(ValidationError):
            pm.add_partial(5, 100.0, date.today(), f)


def test_add_partial_happy_path(monkeypatch):
    cur = _PartialCur(pos_row={"id": 5, "exit_date": None}, partial_rows=[])
    monkeypatch.setattr(pm, "_connect", lambda: _PartialConn(cur))
    new_id = pm.add_partial(5, 123.45, date(2026, 6, 6), 0.5)
    assert new_id == 99
    assert cur.inserted["position_id"] == 5 and cur.inserted["fraction"] == 0.5


def test_add_partial_rejects_closed_position(monkeypatch):
    cur = _PartialCur(pos_row={"id": 5, "exit_date": date(2026, 6, 6)}, partial_rows=[])
    monkeypatch.setattr(pm, "_connect", lambda: _PartialConn(cur))
    with pytest.raises(ValidationError):
        pm.add_partial(5, 100.0, date.today(), 0.5)


def test_add_partial_rejects_over_scale(monkeypatch):
    # 0.7 already scaled out + 0.5 more > 1.0 → reject
    cur = _PartialCur(pos_row={"id": 5, "exit_date": None}, partial_rows=[{"fraction": 0.7}])
    monkeypatch.setattr(pm, "_connect", lambda: _PartialConn(cur))
    with pytest.raises(ValidationError):
        pm.add_partial(5, 100.0, date.today(), 0.5)


# ── update_position (edit) guards ───────────────────────────────────────────

class _EditCur:
    """Serves the position row for update_position; records the UPDATE."""
    def __init__(self, pos_row):
        self._pos_row = pos_row
        self.rowcount = 1
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._pos_row


def _pos_row(**over):
    base = dict(id=11, ticker="TXN", side="long", entry_price=332.28,
                entry_date=date(2026, 6, 22), stop_price=298.92, initial_stop=298.92,
                exit_price=None, exit_date=None, notes=None)
    base.update(over)
    return base


def _edit(monkeypatch, row, **kw):
    cur = _EditCur(row)
    monkeypatch.setattr(pm, "_connect", lambda: _PartialConn(cur))
    ok = pm.update_position(row["id"], **kw)
    return ok, cur


def test_update_position_edits_entry(monkeypatch):
    ok, cur = _edit(monkeypatch, _pos_row(), entry_price=329.99)
    assert ok is True
    sql, params = cur.executed[-1]
    assert "entry_price = %(entry_price)s" in sql and params["entry_price"] == 329.99
    assert params["id"] == 11


def test_update_position_no_fields_raises():
    with pytest.raises(ValidationError):
        pm.update_position(11)


def test_update_position_rejects_exit_on_open(monkeypatch):
    with pytest.raises(ValidationError):
        _edit(monkeypatch, _pos_row(exit_date=None), exit_price=340.0)   # open → no exit edit


def test_update_position_allows_exit_on_closed(monkeypatch):
    ok, cur = _edit(monkeypatch, _pos_row(exit_date=date(2026, 6, 25), exit_price=300.0),
                    exit_price=340.0)
    assert ok is True
    assert cur.executed[-1][1]["exit_price"] == 340.0


def test_update_position_rejects_inverted_initial_stop(monkeypatch):
    # long: initial stop above entry inverts the risk unit
    with pytest.raises(ValidationError):
        _edit(monkeypatch, _pos_row(), initial_stop=340.0)


def test_update_position_rejects_entry_below_initial_stop(monkeypatch):
    # long: editing entry below the initial stop inverts the risk unit
    with pytest.raises(ValidationError):
        _edit(monkeypatch, _pos_row(), entry_price=290.0)   # < initial_stop 298.92


def test_update_position_stop_may_trail_past_entry(monkeypatch):
    # the CURRENT stop is not side-constrained — a long stop above entry (BE/+1R) is fine
    ok, _cur = _edit(monkeypatch, _pos_row(), stop_price=365.64)
    assert ok is True


def test_update_position_notes_only_skips_geometry(monkeypatch):
    # a notes edit must not trip geometry checks even on odd data
    ok, cur = _edit(monkeypatch, _pos_row(initial_stop=None), notes="manual fill")
    assert ok is True
    assert cur.executed[-1][1]["notes"] == "manual fill"


def test_update_position_rejects_bad_price(monkeypatch):
    for bad in (0.0, -5.0):
        with pytest.raises(ValidationError):
            _edit(monkeypatch, _pos_row(), entry_price=bad)


def test_update_position_non_numeric_raises_validation(monkeypatch):
    # a non-numeric price must surface as ValidationError (the documented contract),
    # not a raw ValueError, for direct/programmatic callers
    with pytest.raises(ValidationError):
        _edit(monkeypatch, _pos_row(), entry_price="not-a-number")


def test_remaining_fraction(monkeypatch):
    monkeypatch.setattr(pm, "get_partials", lambda pid: [
        pm.Partial(1, pid, 110.0, date.today(), 0.25),
        pm.Partial(2, pid, 112.0, date.today(), 0.25),
    ])
    assert pm.remaining_fraction(5) == 0.5
    monkeypatch.setattr(pm, "get_partials", lambda pid: [])
    assert pm.remaining_fraction(5) == 1.0


def test_get_partials_maps_rows(monkeypatch):
    # the real body: DB rows → Partial, with float() coercion and ORDER BY id passthrough
    rows = [
        {"id": 1, "position_id": 5, "exit_price": "110.0", "exit_date": date(2026, 6, 6),
         "fraction": "0.25"},
        {"id": 2, "position_id": 5, "exit_price": "112.0", "exit_date": date(2026, 6, 7),
         "fraction": "0.5"},
    ]
    monkeypatch.setattr(pm, "_connect", lambda: _PartialConn(_PartialCur(None, rows)))
    out = pm.get_partials(5)
    assert [(p.id, p.position_id, p.exit_price, p.fraction) for p in out] == \
        [(1, 5, 110.0, 0.25), (2, 5, 112.0, 0.5)]
    assert all(isinstance(p.exit_price, float) and isinstance(p.fraction, float) for p in out)


def test_get_partials_fail_open(monkeypatch):
    import mysql.connector

    def boom():
        raise mysql.connector.Error("db down")
    monkeypatch.setattr(pm, "_connect", boom)
    assert pm.get_partials(5) == []        # fail-open, never raises
