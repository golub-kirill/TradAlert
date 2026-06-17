"""Macro event calendar — surface a DARK gate (audit M9).

The hard-coded fallback list ends 2026-12-31; from 2027 onward (absent a
config/macro_calendar.yaml refresh) every event is in the past and the entry-side
event-risk gate silently stops firing. get_calendar_events must WARN loudly when
that happens.
"""

from __future__ import annotations

from datetime import date

import core.macro.calendar as cal


class _Date2027(date):
    @classmethod
    def today(cls):
        return cls(2027, 6, 1)


class _Date2026(date):
    @classmethod
    def today(cls):
        return cls(2026, 6, 1)


def test_calendar_warns_when_all_events_are_past(monkeypatch, caplog):
    monkeypatch.setattr(cal, "_load_yaml_calendar", lambda: [])  # force hard-coded 2026 list
    monkeypatch.setattr(cal, "date", _Date2027)
    with caplog.at_level("WARNING"):
        events = cal.get_calendar_events()
    assert events                       # the list is still returned (all past)
    assert "DARK" in caplog.text        # but the operator is warned the gate is dark


def test_calendar_quiet_when_events_still_future(monkeypatch, caplog):
    monkeypatch.setattr(cal, "_load_yaml_calendar", lambda: [])
    monkeypatch.setattr(cal, "date", _Date2026)
    with caplog.at_level("WARNING"):
        cal.get_calendar_events()
    assert "DARK" not in caplog.text
