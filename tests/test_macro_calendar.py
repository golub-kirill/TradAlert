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
    monkeypatch.setattr(cal, "_load_tv_calendar", lambda: [])    # skip the live TV feed
    monkeypatch.setattr(cal, "date", _Date2027)
    with caplog.at_level("WARNING"):
        events = cal.get_calendar_events()
    assert events                       # the list is still returned (all past)
    assert "DARK" in caplog.text        # but the operator is warned the gate is dark


def test_calendar_quiet_when_events_still_future(monkeypatch, caplog):
    monkeypatch.setattr(cal, "_load_yaml_calendar", lambda: [])
    monkeypatch.setattr(cal, "_load_tv_calendar", lambda: [])    # skip the live TV feed
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


# ── hold-horizon advisory (events_in_window / event_risk_flags) ───────────────

from core.macro.calendar import events_in_window, event_risk_flags  # noqa: E402


def test_events_in_window_returns_every_event_in_horizon():
    evts = events_in_window(_EVENTS, date(2026, 3, 1), horizon_days=20)
    assert [e.date for e in evts] == [date(2026, 3, 11), date(2026, 3, 17), date(2026, 3, 18)]


def test_event_risk_flags_lists_beyond_the_near_window():
    """An event on day 13 of the horizon must read — the soonest-only 5d flag misses it."""
    flags = event_risk_flags(_EVENTS, date(2026, 3, 4), horizon_days=14)
    assert "CPI in 7d (2026-03-11)" in flags
    assert "FOMC in 13d (2026-03-17)" in flags


def test_event_risk_flags_collapses_consecutive_meeting_days():
    flags = event_risk_flags(_EVENTS, date(2026, 3, 16), horizon_days=5)
    assert flags == "FOMC in 1d (2026-03-17)"      # 03-18 folded into the meeting


def test_event_risk_flags_empty_out_of_horizon():
    assert event_risk_flags(_EVENTS, date(2026, 1, 1), horizon_days=5) == ""


# ── curated-list fact anchors (dates are looked up, never derived) ────────────

def test_hardcoded_cpi_dates_are_the_published_facts():
    """June-2026 CPI released 2026-07-14 (BLS archive). The old 07-15 row came from
    a fictional '2nd Wednesday' rule — this anchor stops any re-derivation."""
    cpi = {e.date for e in cal._HARDCODED_2026 if e.category == "CPI"}
    assert date(2026, 7, 14) in cpi
    assert date(2026, 7, 15) not in cpi
    assert date(2026, 8, 12) in cpi                # next release, BLS-confirmed


def test_hardcoded_fomc_matches_the_fed_page():
    fomc = {e.date for e in cal._HARDCODED_2026 if e.category == "FOMC"}
    assert len(fomc) == 16                          # 8 meetings x 2 days
    assert date(2026, 7, 28) in fomc and date(2026, 7, 29) in fomc


# ── TV-feed divergence check ──────────────────────────────────────────────────

def test_tv_divergence_warns_on_date_mismatch(caplog):
    tv = [CalendarEvent(date(2026, 7, 20), "CPI", "CPI (feed)")]   # curated says 07-14
    with caplog.at_level("WARNING"):
        cal._warn_tv_divergence(tv)
    assert "diverges" in caplog.text and "2026-07-20" in caplog.text


def test_tv_divergence_quiet_on_agreement(caplog):
    tv = [CalendarEvent(date(2026, 7, 14), "CPI", "CPI (feed)")]
    with caplog.at_level("WARNING"):
        cal._warn_tv_divergence(tv)
    assert "diverges" not in caplog.text


def test_tv_divergence_catches_phantom_row_beside_the_real_one():
    """The live-observed failure: the feed carried the correct CPI 07-14 AND a
    phantom 07-20; the soonest-future flag printed the phantom. A superset with
    a wrong extra date must warn."""
    import logging
    tv = [CalendarEvent(date(2026, 7, 14), "CPI", "CPI (feed)"),
          CalendarEvent(date(2026, 7, 20), "CPI", "CPI (feed phantom)")]
    records = []
    h = logging.Handler(); h.emit = lambda r: records.append(r.getMessage())
    cal.logger.addHandler(h)
    try:
        cal._warn_tv_divergence(tv)
    finally:
        cal.logger.removeHandler(h)
    assert any("2026-07-20" in m and "diverges" in m for m in records)


def test_tv_divergence_tolerates_decision_day_only_fomc(caplog):
    tv = [CalendarEvent(date(2026, 7, 29), "FOMC", "rate decision")]  # no day-1 row
    with caplog.at_level("WARNING"):
        cal._warn_tv_divergence(tv)
    assert "diverges" not in caplog.text
