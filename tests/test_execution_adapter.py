"""JournalAdapter delegates to position_manager with exact args; get_adapter is journal-only."""

from __future__ import annotations

from datetime import date

from core.execution import adapter as ad
from core import position_manager as pm


def test_journal_open_delegates(monkeypatch):
    seen = {}

    def fake_open(ticker, entry_price, entry_date, side="long", stop_price=None, notes=""):
        seen.update(ticker=ticker, entry_price=entry_price, entry_date=entry_date,
                    side=side, stop_price=stop_price, notes=notes)
        return 42

    monkeypatch.setattr(pm, "open_position", fake_open)
    rid = ad.JournalAdapter().open("JNJ", 232.77, date(2026, 6, 6),
                                   side="long", stop_price=221.85, notes="tg")
    assert rid == 42
    assert seen == dict(ticker="JNJ", entry_price=232.77, entry_date=date(2026, 6, 6),
                        side="long", stop_price=221.85, notes="tg")


def test_journal_close_and_update_delegate(monkeypatch):
    seen = {}
    monkeypatch.setattr(pm, "close_position", lambda i, p, d: seen.update(close=(i, p, d)) or True)
    monkeypatch.setattr(pm, "update_stop", lambda i, s: seen.update(stop=(i, s)) or True)
    a = ad.JournalAdapter()
    assert a.close(7, 100.0, date(2026, 6, 6)) is True
    assert a.update_stop(7, 95.0) is True
    assert seen["close"] == (7, 100.0, date(2026, 6, 6))
    assert seen["stop"] == (7, 95.0)


def test_get_adapter_is_journal_only():
    # The no-auto-execution guarantee: the factory only ever yields the journal adapter.
    assert isinstance(ad.get_adapter({}), ad.JournalAdapter)
    assert isinstance(ad.get_adapter(), ad.JournalAdapter)
    assert isinstance(ad.get_adapter({"execution": {"adapter": "broker"}}), ad.JournalAdapter)
