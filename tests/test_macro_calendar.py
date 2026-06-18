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


# ── advisory event-risk flag (upcoming_event_risk / event_risk_flag) ──────────────

from core.macro.calendar import (  # noqa: E402
    CalendarEvent, EVENT_RISK_WITHIN_DAYS, event_risk_flag, upcoming_event_risk,
)

_EVENTS = [
    CalendarEvent(date(2026, 3, 11), "CPI", "Feb 2026 CPI release"),
    CalendarEvent(date(2026, 3, 17), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 3, 18), "FOMC", "FOMC decision day + SEP"),
]


def test_upcoming_event_risk_within_window():
    e = upcoming_event_risk(_EVENTS, date(2026, 3, 9), within_days=5)
    assert e is not None and e.category == "CPI" and e.date == date(2026, 3, 11)


def test_upcoming_event_risk_returns_soonest():
    # On 03-12 the CPI is past; the soonest within 5d is the 03-17 FOMC.
    e = upcoming_event_risk(_EVENTS, date(2026, 3, 12), within_days=5)
    assert e is not None and e.date == date(2026, 3, 17)


def test_upcoming_event_risk_none_when_out_of_window():
    assert upcoming_event_risk(_EVENTS, date(2026, 3, 1), within_days=5) is None


def test_upcoming_event_risk_ignores_past_events():
    assert upcoming_event_risk(_EVENTS, date(2026, 3, 19), within_days=30) is None


def test_upcoming_event_risk_includes_today():
    e = upcoming_event_risk(_EVENTS, date(2026, 3, 11), within_days=5)
    assert e is not None and e.date == date(2026, 3, 11)


def test_upcoming_event_risk_category_filter():
    # CPI (03-11) is soonest, but a FOMC-only filter skips it → 03-17.
    e = upcoming_event_risk(_EVENTS, date(2026, 3, 9), within_days=10, categories={"FOMC"})
    assert e is not None and e.category == "FOMC" and e.date == date(2026, 3, 17)


def test_upcoming_event_risk_negative_window_is_none():
    assert upcoming_event_risk(_EVENTS, date(2026, 3, 9), within_days=-1) is None


def test_event_risk_flag_in_n_days():
    assert event_risk_flag(_EVENTS, date(2026, 3, 9), within_days=5) == "CPI in 2d (2026-03-11)"


def test_event_risk_flag_today():
    assert event_risk_flag(_EVENTS, date(2026, 3, 11), within_days=5) == "CPI today"


def test_event_risk_flag_empty_when_none_in_window():
    assert event_risk_flag(_EVENTS, date(2026, 1, 1), within_days=5) == ""


def test_event_risk_default_window_is_five():
    assert EVENT_RISK_WITHIN_DAYS == 5
    # Default window picks up an event 5 days out but not 6.
    assert event_risk_flag(_EVENTS, date(2026, 3, 6)) == "CPI in 5d (2026-03-11)"
    assert event_risk_flag(_EVENTS, date(2026, 3, 5)) == ""
