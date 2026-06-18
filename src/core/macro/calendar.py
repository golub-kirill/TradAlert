"""
Curated calendar events (FOMC / CPI / NFP) for the stop-date gate.

Public API: ``get_calendar_events() -> list[CalendarEvent]``

Source resolution: ``config/macro_calendar.yaml`` if present, else a hard-coded
next-12-month list of FOMC and CPI/NFP dates. FOMC dates are published a year
ahead by the Fed (federalreserve.gov/monetarypolicy/fomccalendars.htm); CPI/NFP
follow the BLS calendar (~08:30 ET).

Consumed by ``main.py`` two ways:
  1. seeds ``FilterEngine._stop_dates`` so a new entry ON one of these dates is
     blocked (held positions still exit normally — the gate is entry-only); and
  2. ``event_risk_flag()`` surfaces an ADVISORY flag on a fresh entry when an
     event falls within the next few days — it never gates or sizes a trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Default look-ahead window (calendar days) for the advisory event-risk flag —
# how far ahead of a fresh entry an FOMC/CPI/NFP is worth surfacing. Advisory
# only (never gates/sizes); the entry-DAY block is the stop_dates gate.
EVENT_RISK_WITHIN_DAYS = 5


@dataclass(frozen=True)
class CalendarEvent:
    """One scheduled macro event the strategy avoids trading into."""
    date: date
    category: str
    description: str
    action: str = "no-trade"


# ── Hard-coded 2026 calendar ─────────────────────────────────────────────────
# FOMC: Federal Reserve published schedule.
# CPI: BLS releases on the 2nd Wednesday of the month (08:30 ET).
# NFP: BLS first Friday of each month at 08:30 ET.
_HARDCODED_2026: tuple[CalendarEvent, ...] = (
    CalendarEvent(date(2026, 1, 27), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 1, 28), "FOMC", "FOMC decision day"),
    CalendarEvent(date(2026, 3, 17), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 3, 18), "FOMC", "FOMC decision day + SEP"),
    CalendarEvent(date(2026, 4, 28), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 4, 29), "FOMC", "FOMC decision day"),
    CalendarEvent(date(2026, 6, 16), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 6, 17), "FOMC", "FOMC decision day + SEP"),
    CalendarEvent(date(2026, 7, 28), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 7, 29), "FOMC", "FOMC decision day"),
    CalendarEvent(date(2026, 9, 15), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 9, 16), "FOMC", "FOMC decision day + SEP"),
    CalendarEvent(date(2026, 10, 27), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 10, 28), "FOMC", "FOMC decision day"),
    CalendarEvent(date(2026, 12, 8), "FOMC", "FOMC meeting day 1"),
    CalendarEvent(date(2026, 12, 9), "FOMC", "FOMC decision day + SEP"),
    CalendarEvent(date(2026, 1, 14), "CPI", "Dec 2025 CPI release"),
    CalendarEvent(date(2026, 2, 11), "CPI", "Jan 2026 CPI release"),
    CalendarEvent(date(2026, 3, 11), "CPI", "Feb 2026 CPI release"),
    CalendarEvent(date(2026, 4, 14), "CPI", "Mar 2026 CPI release"),
    CalendarEvent(date(2026, 5, 13), "CPI", "Apr 2026 CPI release"),
    CalendarEvent(date(2026, 6, 10), "CPI", "May 2026 CPI release"),
    CalendarEvent(date(2026, 7, 15), "CPI", "Jun 2026 CPI release"),
    CalendarEvent(date(2026, 8, 12), "CPI", "Jul 2026 CPI release"),
    CalendarEvent(date(2026, 9, 10), "CPI", "Aug 2026 CPI release"),
    CalendarEvent(date(2026, 10, 14), "CPI", "Sep 2026 CPI release"),
    CalendarEvent(date(2026, 11, 12), "CPI", "Oct 2026 CPI release"),
    CalendarEvent(date(2026, 12, 10), "CPI", "Nov 2026 CPI release"),
    CalendarEvent(date(2026, 1, 2), "NFP", "Dec 2025 jobs report"),
    CalendarEvent(date(2026, 2, 6), "NFP", "Jan 2026 jobs report"),
    CalendarEvent(date(2026, 3, 6), "NFP", "Feb 2026 jobs report"),
    CalendarEvent(date(2026, 4, 3), "NFP", "Mar 2026 jobs report"),
    CalendarEvent(date(2026, 5, 1), "NFP", "Apr 2026 jobs report"),
    CalendarEvent(date(2026, 6, 5), "NFP", "May 2026 jobs report"),
    CalendarEvent(date(2026, 7, 2), "NFP", "Jun 2026 jobs report"),
    CalendarEvent(date(2026, 8, 7), "NFP", "Jul 2026 jobs report"),
    CalendarEvent(date(2026, 9, 4), "NFP", "Aug 2026 jobs report"),
    CalendarEvent(date(2026, 10, 2), "NFP", "Sep 2026 jobs report"),
    CalendarEvent(date(2026, 11, 6), "NFP", "Oct 2026 jobs report"),
    CalendarEvent(date(2026, 12, 4), "NFP", "Nov 2026 jobs report"),
)


def get_calendar_events(
        *, categories: Iterable[str] | None = None,
) -> list[CalendarEvent]:
    """Return the rolling-year list of stop-date macro events.

    Resolution order:
      1. ``config/macro_calendar.yaml`` (maintainer override).
      2. Hard-coded 2026 list above (offline-fallback).
    """
    events: list[CalendarEvent] = []
    try:
        events = _load_yaml_calendar()
    except (OSError, ValueError, KeyError) as exc:
        logger.debug(
            "[calendar] YAML override unavailable (%s); "
            "falling back to hard-coded list", exc,
        )

    if not events:
        events = list(_HARDCODED_2026)

    if categories is not None:
        whitelist = {c.upper() for c in categories}
        events = [e for e in events if e.category.upper() in whitelist]

    events.sort(key=lambda e: e.date)

    # Flag an exhausted calendar loudly: the hard-coded fallback ends 2026-12-31,
    # so from 2027 (absent a config/macro_calendar.yaml refresh) every event is
    # past and the entry-side event-risk gate goes dark.
    today = date.today()
    if not events:
        logger.warning("[calendar] no macro events available — the event-risk "
                       "gate is DARK; add config/macro_calendar.yaml.")
    elif max(e.date for e in events) < today:
        logger.warning(
            "[calendar] all %d macro events are in the past (latest %s) — the "
            "event-risk gate is DARK; refresh config/macro_calendar.yaml or "
            "extend the hard-coded list.", len(events), max(e.date for e in events))

    return events


def upcoming_event_risk(
        events: Iterable[CalendarEvent], today: date, *,
        within_days: int = EVENT_RISK_WITHIN_DAYS,
        categories: Iterable[str] | None = None,
) -> CalendarEvent | None:
    """Nearest scheduled macro event in ``[today, today + within_days]`` (inclusive), or None.

    Pure + deterministic (``today`` passed explicitly, ``events`` is a
    ``get_calendar_events()`` list) so it is unit-testable. ADVISORY ONLY: never
    gates or sizes a trade (the entry-DAY no-trade block is the ``stop_dates``
    gate). ``today`` is included so a same-day event still reads. Ties on the
    soonest date resolve to list order.
    """
    if within_days < 0:
        return None
    horizon = today + timedelta(days=within_days)
    whitelist = {c.upper() for c in categories} if categories is not None else None
    upcoming = [
        e for e in events
        if today <= e.date <= horizon
        and (whitelist is None or e.category.upper() in whitelist)
    ]
    if not upcoming:
        return None
    return min(upcoming, key=lambda e: e.date)


def event_risk_flag(
        events: Iterable[CalendarEvent], today: date, *,
        within_days: int = EVENT_RISK_WITHIN_DAYS,
        categories: Iterable[str] | None = None,
) -> str:
    """Formatted advisory flag for the soonest upcoming macro event, or "" if none in window.

    e.g. ``"FOMC today"``, ``"CPI in 1d (2026-02-11)"``, ``"FOMC in 3d (2026-03-18)"``. Thin
    display wrapper over :func:`upcoming_event_risk` — the single source of the flag string.
    """
    evt = upcoming_event_risk(events, today, within_days=within_days, categories=categories)
    if evt is None:
        return ""
    days = (evt.date - today).days
    when = "today" if days == 0 else f"in {days}d ({evt.date.isoformat()})"
    return f"{evt.category} {when}"


def _load_yaml_calendar() -> list[CalendarEvent]:
    """Read ``config/macro_calendar.yaml`` if it exists."""
    import yaml
    cfg_path = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "config" / "macro_calendar.yaml"
    )
    if not cfg_path.exists():
        return []
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = data.get("events", []) or []
    out: list[CalendarEvent] = []
    for row in raw:
        try:
            d = date.fromisoformat(str(row["date"]))
            out.append(CalendarEvent(
                date=d,
                category=str(row.get("category", "MISC")),
                description=str(row.get("description", "")),
                action=str(row.get("action", "no-trade")),
            ))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("[calendar] skipping malformed event row %r: %s",
                           row, exc)
    return out
