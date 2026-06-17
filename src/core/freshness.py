"""
core/freshness.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Live data-freshness guards — so the LIVE scanner never evaluates a signal on a partial
(unclosed) bar or on stale data, and an overnight/weekend gap downgrades a fire to
NEEDS_REVIEW instead of LIVE. See DESIGN §0 vector C/G + TODO "Live data-freshness hardening".

**LIVE PATH ONLY.** The daily backtester replays COMPLETED end-of-day bars by construction, so
none of this touches it — the run_id=15 headline stays byte-identical. These are pure helpers
(trading-day calendar math, gap arithmetic); the live wiring in ``main.py`` decides what to do
with the verdicts (refetch on stale, skip if still stale, mark NEEDS_REVIEW on a gap).

Exchange awareness: US tickers use the NYSE calendar, ``.TO`` names the TSX calendar (different
holidays — Canada Day, Canadian Thanksgiving, etc.), via ``pandas_market_calendars``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pandas_market_calendars as mcal

_TSX_SUFFIX = ".TO"
_LOOKBACK_DAYS = 15  # calendar days of schedule to inspect — covers any holiday run
_cal_cache: dict[str, "mcal.MarketCalendar"] = {}


def exchange_for(ticker: str) -> str:
    """Exchange calendar name for a ticker: ``TSX`` for ``.TO`` names, else ``NYSE``."""
    return "TSX" if ticker.upper().endswith(_TSX_SUFFIX) else "NYSE"


def _calendar(exchange: str):
    cal = _cal_cache.get(exchange)
    if cal is None:
        cal = mcal.get_calendar(exchange)
        _cal_cache[exchange] = cal
    return cal


def _as_utc(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def last_completed_session(now: datetime, exchange: str = "NYSE") -> date:
    """Date of the most recent exchange session whose ``market_close`` is ``<= now``.

    A still-open (or not-yet-opened) current session is excluded, so a mid-session run
    resolves to the *prior* session — the last fully-formed daily bar. ``now`` is treated as
    UTC when tz-naive.
    """
    now = _as_utc(now)
    cal = _calendar(exchange)
    start = (now - pd.Timedelta(days=_LOOKBACK_DAYS)).date()
    sched = cal.schedule(start_date=start, end_date=now.date())
    if len(sched) == 0:
        return now.date()
    closes = sched["market_close"]
    completed = closes[closes <= pd.Timestamp(now)]
    if len(completed) == 0:
        return sched.index[0].date()  # before the window's first close — clamp to it
    return completed.index[-1].date()


def drop_unclosed_bar(df: pd.DataFrame, now: datetime,
                      exchange: str = "NYSE") -> pd.DataFrame:
    """Drop trailing rows dated AFTER the last completed session (an unclosed current-day
    bar from a ``end=today+1d`` fetch). No-op when the last bar is already a completed
    session. Index assumed tz-naive daily timestamps (the loader strips tz)."""
    if df is None or len(df) == 0:
        return df
    lcs = last_completed_session(now, exchange)
    if df.index[-1].normalize() <= pd.Timestamp(lcs):
        return df
    return df[df.index.normalize() <= pd.Timestamp(lcs)]


def sessions_behind(last_bar: date, now: datetime, exchange: str = "NYSE") -> int:
    """How many completed sessions the data is behind: 0 = fresh (last bar == last completed
    session), >= 1 = stale by that many sessions. A bar at/after the last completed session
    returns 0."""
    lcs = last_completed_session(now, exchange)
    if last_bar >= lcs:
        return 0
    cal = _calendar(exchange)
    valid = cal.valid_days(start_date=last_bar, end_date=lcs)  # inclusive both ends
    return max(0, len(valid) - 1)


def is_stale(last_bar: date, now: datetime, exchange: str = "NYSE",
             max_sessions: int = 1) -> bool:
    """True when the data is ``>= max_sessions`` completed sessions behind the last close.
    ``max_sessions=1`` (default): anything older than the last completed session is stale."""
    return sessions_behind(last_bar, now, exchange) >= max_sessions


def overnight_gap(live_price: float | None, last_close: float | None,
                  atr: float | None, atr_mult: float = 2.0) -> tuple[float, float, bool]:
    """Overnight/weekend gap between the last daily close and the current live price.

    Returns ``(gap_abs, gap_pct, breached)`` where ``breached = |live − last_close| >
    atr_mult × ATR`` (a stale-entry / news-gap flag → the caller marks the signal
    NEEDS_REVIEW). Returns ``(nan, nan, False)`` on missing/invalid inputs (fail-open — a
    missing live price must not fabricate a breach)."""
    try:
        live = float(live_price)
        lc = float(last_close)
        a = float(atr)
    except (TypeError, ValueError):
        return float("nan"), float("nan"), False
    if not (lc > 0.0) or not (a > 0.0) or live != live:  # last guards NaN live
        return float("nan"), float("nan"), False
    gap = live - lc
    return gap, gap / lc, abs(gap) > atr_mult * a
