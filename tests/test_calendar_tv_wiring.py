"""get_calendar_events() ← TradingView feed wiring (S6 Part B).

Hermetic: the live TV fetch (core.fetchers.macro.tv_calendar.fetch_tv_calendar) is
monkeypatched in every test — zero network. Verifies the resolution order
yaml → TV → hard-coded, the fail-open fallbacks, and the TV-row → CalendarEvent mapping.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

import core.macro.calendar as cal

_TV = "core.fetchers.macro.tv_calendar.fetch_tv_calendar"  # patched (lazy-imported in _load_tv_calendar)


@pytest.fixture(autouse=True)
def _no_yaml(monkeypatch):
    """Force the TV path: stub the yaml override to empty so it never shadows the feed."""
    monkeypatch.setattr(cal, "_load_yaml_calendar", lambda: [])


def _tv_df(n_per=12):
    """Synthetic rolling-year TV frame: n_per each of FOMC/CPI/NFP, extending into next year."""
    today = date.today()
    rows = []
    for i in range(n_per):
        base = today + timedelta(days=20 + i * 30)
        rows.append({"date": base, "category": "FOMC", "title": "Fed Interest Rate Decision"})
        rows.append({"date": base + timedelta(days=2), "category": "CPI", "title": "Inflation Rate YoY"})
        rows.append({"date": base + timedelta(days=4), "category": "NFP", "title": "Non Farm Payrolls"})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_prefers_tv_feed_when_present(monkeypatch, caplog):
    monkeypatch.setattr(_TV, lambda *a, **k: _tv_df())
    with caplog.at_level("WARNING"):
        ev = cal.get_calendar_events()
    assert len(ev) >= 36
    assert {e.category for e in ev} <= {"FOMC", "CPI", "NFP"}
    # TV extends past the hard-coded 2026 list → a next-year event is present…
    assert any(e.date.year >= 2027 for e in ev)
    # …and because the feed covers the future, the DARK gate must NOT warn.
    assert "DARK" not in caplog.text


def test_tv_failure_falls_back_to_hardcoded(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(_TV, _boom)
    ev = cal.get_calendar_events()
    assert len(ev) >= 36                          # the offline hard-coded list
    assert all(e.date.year == 2026 for e in ev)   # _HARDCODED_2026 is all-2026


def test_tv_empty_frame_falls_back(monkeypatch):
    monkeypatch.setattr(_TV, lambda *a, **k: pd.DataFrame(columns=["date", "category", "title"]))
    ev = cal.get_calendar_events()
    assert len(ev) >= 36 and all(e.date.year == 2026 for e in ev)


def test_tv_rows_map_to_calendar_events(monkeypatch):
    when = date.today() + timedelta(days=30)
    df = pd.DataFrame([{"date": pd.Timestamp(when), "category": "FOMC",
                        "title": "Fed Interest Rate Decision"}])
    monkeypatch.setattr(_TV, lambda *a, **k: df)
    ev = cal.get_calendar_events()
    assert len(ev) == 1
    e = ev[0]
    assert e.date == when
    assert e.category == "FOMC"
    assert e.description == "Fed Interest Rate Decision"
    assert e.action == "no-trade"


def test_load_tv_calendar_swallows_any_exception(monkeypatch):
    def _boom(*a, **k):
        raise Exception("boom")
    monkeypatch.setattr(_TV, _boom)
    assert cal._load_tv_calendar() == []   # never raises → []
