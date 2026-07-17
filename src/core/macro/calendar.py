"""
Curated calendar events (FOMC / CPI / NFP) for the stop-date gate.

Public API: ``get_calendar_events() -> list[CalendarEvent]``

Source resolution: ``config/macro_calendar.yaml`` (maintainer override) if present,
else the live TradingView feed (``tv_calendar`` — cached + fail-open, forward-extending
so it survives the 2027 sunset of the hard-coded list), else a hard-coded 2026 list.
FOMC dates are published a year ahead by the Fed
(federalreserve.gov/monetarypolicy/fomccalendars.htm); CPI/NFP follow the BLS
calendar (~08:30 ET).

Consumed by ``main.py`` for the ADVISORY event-risk flag only:
``event_risk_flag()`` surfaces a flag on a fresh entry when an FOMC/CPI/NFP
falls within the next few days — it never gates or sizes a trade. These curated
dates do NOT seed ``FilterEngine._stop_dates`` (a macro-event entry block A/B'd
as near-negligible, so it stays advisory here). The engine's hard entry-day
blackout is the separate, manually-curated ``events.stop_dates`` list in
``config/filters.yaml``.
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


# ── Hard-coded 2026 calendar (offline fallback) ──────────────────────────────
# Release dates are published facts — looked up, never derived from a weekday
# pattern (BLS shifts for holidays and reschedules; e.g. the Jan-2026 CPI moved
# 02-11 → 02-13, and the Jun-2026 CPI landed on a Tuesday).
# FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm  (verified 2026-07-17)
# CPI:  bls.gov/schedule/news_release/cpi.htm    (trued-up 2026-07-17; Jun-2026
#       release = 07-14 per the BLS news-release archive)
# NFP:  bls.gov/schedule/news_release/empsit.htm (02-06 / 08-07 anchors verified)
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
    CalendarEvent(date(2026, 1, 13), "CPI", "Dec 2025 CPI release"),
    CalendarEvent(date(2026, 2, 13), "CPI", "Jan 2026 CPI release"),  # resched. from 02-11
    CalendarEvent(date(2026, 3, 11), "CPI", "Feb 2026 CPI release"),
    CalendarEvent(date(2026, 4, 10), "CPI", "Mar 2026 CPI release"),
    CalendarEvent(date(2026, 5, 12), "CPI", "Apr 2026 CPI release"),
    CalendarEvent(date(2026, 6, 10), "CPI", "May 2026 CPI release"),
    CalendarEvent(date(2026, 7, 14), "CPI", "Jun 2026 CPI release"),
    CalendarEvent(date(2026, 8, 12), "CPI", "Jul 2026 CPI release"),
    CalendarEvent(date(2026, 9, 11), "CPI", "Aug 2026 CPI release"),
    CalendarEvent(date(2026, 10, 14), "CPI", "Sep 2026 CPI release"),
    CalendarEvent(date(2026, 11, 10), "CPI", "Oct 2026 CPI release"),
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
    """Return the rolling-year list of macro events (advisory event-risk feed).

    Resolution order (each step fail-open — a failure falls through to the next):
      1. ``config/macro_calendar.yaml`` (maintainer override; wins when present).
      2. Live TradingView feed (``tv_calendar`` — cached, forward-extending).
      3. Hard-coded 2026 list above (offline fallback; dark from 2027).
    """
    events: list[CalendarEvent] = []
    try:
        events = _load_yaml_calendar()
    except (OSError, ValueError, KeyError) as exc:
        logger.debug(
            "[calendar] YAML override unavailable (%s); trying the TV feed", exc,
        )

    if not events:
        events = _load_tv_calendar()
        if events:
            try:
                _warn_tv_divergence(events)
            except Exception as exc:  # noqa: BLE001 - advisory check, never fatal
                logger.debug("[calendar] divergence check failed: %s", exc)

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


def events_in_window(
        events: Iterable[CalendarEvent], today: date, *,
        horizon_days: int,
        categories: Iterable[str] | None = None,
) -> list[CalendarEvent]:
    """Every scheduled event in ``[today, today + horizon_days]`` (inclusive), soonest first.

    Unlike :func:`upcoming_event_risk` (soonest-only, near-window) this returns the
    full set, so a caller can cover a trade's whole expected hold — an FOMC on day
    14 of a 3–14d hold must read even though the 5-day near-window misses it.
    """
    if horizon_days < 0:
        return []
    horizon = today + timedelta(days=horizon_days)
    whitelist = {c.upper() for c in categories} if categories is not None else None
    hits = [
        e for e in events
        if today <= e.date <= horizon
        and (whitelist is None or e.category.upper() in whitelist)
    ]
    return sorted(hits, key=lambda e: e.date)


def event_risk_flags(
        events: Iterable[CalendarEvent], today: date, *,
        horizon_days: int,
        categories: Iterable[str] | None = None,
) -> str:
    """Advisory flag listing EVERY event inside the horizon, ``""`` when none.

    e.g. ``"CPI in 2d (2026-08-12); FOMC in 12d (2026-08-22)"``. Consecutive
    same-category days (the two-day FOMC meeting) collapse to the first day.
    """
    hits = events_in_window(events, today, horizon_days=horizon_days, categories=categories)
    parts: list[str] = []
    prev: CalendarEvent | None = None
    for e in hits:
        if prev is not None and e.category == prev.category and (e.date - prev.date).days == 1:
            prev = e  # second day of a multi-day meeting — already flagged
            continue
        days = (e.date - today).days
        when = "today" if days == 0 else f"in {days}d ({e.date.isoformat()})"
        parts.append(f"{e.category} {when}")
        prev = e
    return "; ".join(parts)


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


def _warn_tv_divergence(tv_events: list[CalendarEvent]) -> None:
    """Warn loudly when the TV feed carries a date the curated list does not.

    Per (category, month) bucket: any feed date absent from the curated set is
    flagged — this catches both a wrong date AND a mislabeled extra row beside
    the right one (the feed shipped a phantom "CPI 2026-07-20" alongside the
    real 07-14, and the soonest-future flag then printed the phantom). A feed
    that carries a SUBSET (e.g. FOMC decision day without meeting day 1) is
    tolerated. Advisory only; the operator verifies against the publisher.
    """
    def by_month(events: Iterable[CalendarEvent]) -> dict[tuple, set[date]]:
        idx: dict[tuple, set[date]] = {}
        for e in events:
            idx.setdefault((e.category.upper(), e.date.year, e.date.month), set()).add(e.date)
        return idx

    tv, ref = by_month(tv_events), by_month(_HARDCODED_2026)
    diverged = []
    for key in sorted(set(tv) & set(ref)):
        extra = sorted(tv[key] - ref[key])
        if extra:
            cat, y, m = key
            diverged.append(
                f"{cat} {y}-{m:02d}: feed-only {', '.join(map(str, extra))} "
                f"(curated: {', '.join(map(str, sorted(ref[key])))})")
    if diverged:
        logger.warning(
            "[calendar] TV feed diverges from the curated list on %d bucket(s): %s "
            "— verify against the publisher (bls.gov / federalreserve.gov) before "
            "trusting either.", len(diverged), "; ".join(diverged))


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


def _load_tv_calendar() -> list[CalendarEvent]:
    """Rolling-year FOMC/CPI/NFP dates from the TradingView feed, or [] on any failure.

    Live-only + fail-open: the backtester never calls this (no engine/backtest module
    imports this package), and any fetch/parse error returns [] so
    ``get_calendar_events`` falls through to the hard-coded list. The feed is cached
    and forward-extending, so it survives the 2027 sunset of ``_HARDCODED_2026``.
    """
    try:
        from core.fetchers.macro.tv_calendar import fetch_tv_calendar
        today = date.today()
        # today−7 so a just-passed same-week event still reads; +400d ≈ a full rolling
        # year of slack so the DARK check never trips while the feed is live.
        df = fetch_tv_calendar(today - timedelta(days=7), today + timedelta(days=400))
    except Exception as exc:  # noqa: BLE001 - fail-open; never break a live scan
        logger.debug("[calendar] TV feed unavailable (%s); trying hard-coded list", exc)
        return []
    if df is None or df.empty:
        return []
    out: list[CalendarEvent] = []
    for row in df.itertuples(index=False):
        try:
            out.append(CalendarEvent(
                date=row.date.date(),
                category=str(row.category),
                description=str(row.title),
            ))
        except (AttributeError, ValueError, TypeError) as exc:
            logger.debug("[calendar] skipping malformed TV row %r: %s", row, exc)
    return out
