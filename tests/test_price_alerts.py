"""Price alerts — DB layer, cross logic, daemon commands, and the poll sweep.

No real DB / no network: the connection is a fake cursor; async handlers run via
asyncio.run with duck-typed PTB fakes.
"""
from __future__ import annotations

import asyncio

import pytest

import telegram_bot as tb
from persistence import db

_OWNER = 4242


# ── fake DB cursor/conn ───────────────────────────────────────────────────────

class _Cur:
    def __init__(self, *, lastrowid=0, rowcount=0, rows=None):
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self._rows = rows or []
        self.calls: list = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, cur):
        self._c = cur

    def cursor(self):
        return self._c

    def commit(self):
        pass

    def is_connected(self):
        return True

    def close(self):
        pass


# ── DB layer ──────────────────────────────────────────────────────────────────

def test_add_price_alert_inserts_upper_and_returns_id(monkeypatch):
    cur = _Cur(lastrowid=42)
    monkeypatch.setattr(db, "_connect", lambda: _Conn(cur))
    assert db.add_price_alert("nvda", "above", 150.5) == 42
    _sql, params = cur.calls[0]
    assert params == {"ticker": "NVDA", "direction": "above", "price": 150.5}


def test_add_price_alert_rejects_bad_direction(monkeypatch):
    touched = []
    monkeypatch.setattr(db, "_connect", lambda: touched.append(1))
    assert db.add_price_alert("NVDA", "sideways", 10) is None
    assert touched == []                       # never opened a connection


def test_list_price_alerts_maps_rows(monkeypatch):
    cur = _Cur(rows=[(1, "NVDA", "above", 150.0), (2, "TSLA", "below", 200.0)])
    monkeypatch.setattr(db, "_connect", lambda: _Conn(cur))
    got = [(a.id, a.ticker, a.direction, a.price) for a in db.list_price_alerts()]
    assert got == [(1, "NVDA", "above", 150.0), (2, "TSLA", "below", 200.0)]


def test_deactivate_cancel_vs_fire_sql(monkeypatch):
    cur = _Cur(rowcount=1)
    monkeypatch.setattr(db, "_connect", lambda: _Conn(cur))
    assert db.deactivate_price_alert(5) is True
    assert "fired_at" not in cur.calls[0][0]            # plain cancel leaves fired_at NULL
    cur2 = _Cur(rowcount=1)
    monkeypatch.setattr(db, "_connect", lambda: _Conn(cur2))
    assert db.deactivate_price_alert(5, fired=True) is True
    assert "fired_at" in cur2.calls[0][0]


def test_db_ops_fail_open(monkeypatch):
    def boom():
        raise db.MySQLError("db down")
    monkeypatch.setattr(db, "_connect", boom)
    assert db.add_price_alert("NVDA", "above", 10) is None
    assert db.list_price_alerts() == []
    assert db.deactivate_price_alert(1) is False


# ── pure cross logic ──────────────────────────────────────────────────────────

def test_alert_crossed():
    assert tb._alert_crossed("above", 100, 100) is True
    assert tb._alert_crossed("above", 100, 99.99) is False
    assert tb._alert_crossed("below", 100, 100) is True
    assert tb._alert_crossed("below", 100, 100.01) is False
    assert tb._alert_crossed("sideways", 100, 100) is False


def test_market_gate_returns_bool():
    assert isinstance(tb._us_market_open_now(), bool)


# ── PTB fakes + commands ──────────────────────────────────────────────────────

class _Msg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)


class _Upd:
    def __init__(self, *, uid=_OWNER):
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": 1})()
        self.callback_query = None
        self.message = _Msg()


class _Ctx:
    def __init__(self, args=None):
        self.args = args or []
        self.bot = None


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)


def test_cmd_alert_set(monkeypatch):
    monkeypatch.setattr("persistence.db.add_price_alert", lambda t, d, p: 7)
    upd = _Upd()
    asyncio.run(tb.cmd_alert(upd, _Ctx(args=["nvda", "above", "150"])))
    assert any("#7" in t for t in upd.message.texts)


def test_cmd_alert_del(monkeypatch):
    seen = []
    monkeypatch.setattr("persistence.db.deactivate_price_alert",
                        lambda i: (seen.append(i), True)[1])
    upd = _Upd()
    asyncio.run(tb.cmd_alert(upd, _Ctx(args=["del", "5"])))
    assert seen == [5] and any("removed" in t for t in upd.message.texts)


def test_cmd_alert_usage_and_bad_price(monkeypatch):
    upd = _Upd()
    asyncio.run(tb.cmd_alert(upd, _Ctx(args=["NVDA"])))
    assert any("usage:" in t for t in upd.message.texts)
    upd2 = _Upd()
    asyncio.run(tb.cmd_alert(upd2, _Ctx(args=["NVDA", "above", "xx"])))
    assert any("bad price" in t for t in upd2.message.texts)


def test_cmd_alerts_list_and_empty(monkeypatch):
    monkeypatch.setattr("persistence.db.list_price_alerts",
                        lambda: [db.PriceAlert(1, "NVDA", "above", 150.0)])
    upd = _Upd()
    asyncio.run(tb.cmd_alerts(upd, _Ctx()))
    assert any("NVDA" in t and "#1" in t for t in upd.message.texts)
    monkeypatch.setattr("persistence.db.list_price_alerts", lambda: [])
    upd2 = _Upd()
    asyncio.run(tb.cmd_alerts(upd2, _Ctx()))
    assert any("no active" in t for t in upd2.message.texts)


# ── poll sweep ────────────────────────────────────────────────────────────────

def _stub_notify(monkeypatch, sink):
    async def _n(bot, a, px):
        sink.append((a.id, px))
    monkeypatch.setattr(tb, "_notify_alert", _n)


def test_poll_fires_and_deactivates_on_cross(monkeypatch):
    monkeypatch.setattr("persistence.db.list_price_alerts",
                        lambda: [db.PriceAlert(1, "NVDA", "above", 100.0)])
    deact = []
    monkeypatch.setattr("persistence.db.deactivate_price_alert",
                        lambda i, *, fired=False: deact.append((i, fired)))
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 105.0)
    notified = []
    _stub_notify(monkeypatch, notified)
    asyncio.run(tb._alert_poll_once(bot=object()))
    assert notified == [(1, 105.0)]
    assert deact == [(1, True)]


def test_poll_no_fire_when_not_crossed(monkeypatch):
    monkeypatch.setattr("persistence.db.list_price_alerts",
                        lambda: [db.PriceAlert(1, "NVDA", "above", 100.0)])
    deact = []
    monkeypatch.setattr("persistence.db.deactivate_price_alert",
                        lambda *a, **k: deact.append(1))
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 95.0)
    notified = []
    _stub_notify(monkeypatch, notified)
    asyncio.run(tb._alert_poll_once(bot=object()))
    assert notified == [] and deact == []


def test_poll_dedups_price_fetch_per_ticker(monkeypatch):
    monkeypatch.setattr("persistence.db.list_price_alerts",
                        lambda: [db.PriceAlert(1, "NVDA", "above", 100.0),
                                 db.PriceAlert(2, "NVDA", "below", 90.0)])
    monkeypatch.setattr("persistence.db.deactivate_price_alert", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: (calls.append(t), 105.0)[1])
    _stub_notify(monkeypatch, [])
    asyncio.run(tb._alert_poll_once(bot=object()))
    assert calls == ["NVDA"]                    # one fetch despite two alerts on the ticker
