"""
DB-down operator notice (log-scan follow-up): when MySQL is unreachable the scan
runs fail-open (no guard), but the operator is NOTIFIED. Covers db_reachable()
detection and the fail-open send_notice() path.
"""

from __future__ import annotations

from core import position_manager as pm
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


def test_send_notice_noop_when_disabled():
    # No raise, no send when telegram is disabled.
    send_notice("hi", {"telegram": {"enabled": False}})


def test_send_notice_noop_when_token_missing(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    # Enabled but no token -> logs + returns fail-open, never raises.
    send_notice("hi", {"telegram": {"enabled": True}})
