"""
DB-down operator notice (log-scan follow-up): when MySQL is unreachable the scan
runs fail-open (no guard), but the operator is NOTIFIED. Covers db_reachable()
detection and the fail-open send_notice() path.
"""

from __future__ import annotations

from core import position_manager as pm
from core.telegram import push
from core.telegram.push import send_notice


def test_db_reachable_false_on_connect_error(monkeypatch):
    from mysql.connector import Error as MySQLError

    def boom():
        raise MySQLError("can't connect")

    monkeypatch.setattr(pm, "_connect", boom)
    assert pm.db_reachable() is False


def test_db_reachable_true_when_connected(monkeypatch):
    class _Conn:
        def is_connected(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(pm, "_connect", lambda: _Conn())
    assert pm.db_reachable() is True


def test_send_notice_noop_when_disabled(monkeypatch):
    # A valid token IS present, so ONLY the enabled=False guard prevents a send.
    # Record asyncio.run: removing the disabled short-circuit makes it fire -> red.
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "123")
    calls = []
    monkeypatch.setattr(push.asyncio, "run", lambda *a, **k: calls.append(1))
    send_notice("hi", {"telegram": {"enabled": False}})
    assert calls == []   # nothing dispatched when disabled


def test_send_notice_noop_when_token_missing(monkeypatch, caplog):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    calls = []
    monkeypatch.setattr(push.asyncio, "run", lambda *a, **k: calls.append(1))
    with caplog.at_level("WARNING"):
        send_notice("hi", {"telegram": {"enabled": True}})
    assert calls == []   # enabled but no token -> never dispatched
    # the missing-token short-circuit specifically fired (not the not-numeric path)
    assert any("TG_BOT_TOKEN/TG_CHAT_ID missing" in r.message for r in caplog.records)
