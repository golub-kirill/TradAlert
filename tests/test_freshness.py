"""Unit tests for core.freshness (live data-freshness guards) — pure calendar/gap math,
no network, runs in the normal suite. Asserts against the REAL NYSE/TSX calendars so the
holiday/weekend logic (item 3's whole point) is verified, not assumed.
"""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from core.freshness import (  # noqa: E402
    drop_unclosed_bar, exchange_for, is_stale, last_completed_session,
    overnight_gap, sessions_behind,
)

UTC = timezone.utc
# June 2026 NYSE sessions: Mon 6-08, Tue 6-09, Wed 6-10, Thu 6-11, Fri 6-12, (wknd), Mon 6-15.
POST_CLOSE_0615 = datetime(2026, 6, 15, 22, 0, tzinfo=UTC)   # Mon, after the 20:00 UTC close
MID_0615 = datetime(2026, 6, 15, 13, 0, tzinfo=UTC)          # Mon pre-market / midday
WEEKEND = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)           # Saturday


def test_exchange_for():
    assert exchange_for("SHOP.TO") == "TSX"
    assert exchange_for("aapl") == "NYSE"


def test_last_completed_session_postclose_is_today():
    assert last_completed_session(POST_CLOSE_0615, "NYSE") == date(2026, 6, 15)


def test_last_completed_session_midsession_is_prior():
    assert last_completed_session(MID_0615, "NYSE") == date(2026, 6, 12)


def test_last_completed_session_weekend_is_friday():
    assert last_completed_session(WEEKEND, "NYSE") == date(2026, 6, 12)


def _daily_df(last_day: str) -> pd.DataFrame:
    idx = pd.bdate_range("2026-05-01", last_day)
    return pd.DataFrame({"close": np.arange(len(idx), dtype=float)}, index=idx)


def test_drop_unclosed_bar_drops_partial_today():
    out = drop_unclosed_bar(_daily_df("2026-06-15"), MID_0615, "NYSE")
    assert out.index[-1].date() == date(2026, 6, 12)   # the partial Mon 6-15 bar dropped


def test_drop_unclosed_bar_noop_when_last_is_completed():
    df = _daily_df("2026-06-12")
    out = drop_unclosed_bar(df, POST_CLOSE_0615, "NYSE")  # stale, but not partial → untouched
    assert out.index[-1].date() == date(2026, 6, 12) and len(out) == len(df)


def test_sessions_behind_and_is_stale():
    assert sessions_behind(date(2026, 6, 15), POST_CLOSE_0615, "NYSE") == 0
    assert sessions_behind(date(2026, 6, 12), POST_CLOSE_0615, "NYSE") == 1   # Fri vs Mon close
    assert sessions_behind(date(2026, 6, 10), POST_CLOSE_0615, "NYSE") == 3   # Wed → Mon
    assert is_stale(date(2026, 6, 12), POST_CLOSE_0615, "NYSE") is True
    assert is_stale(date(2026, 6, 15), POST_CLOSE_0615, "NYSE") is False


def test_friday_data_not_stale_midsession_monday():
    # item 3: a weekend/holiday gap must NOT read as stale — Friday is the last completed
    # session mid-Monday, so Friday data is fresh until Monday's close.
    assert is_stale(date(2026, 6, 12), MID_0615, "NYSE") is False


def test_holiday_divergence_nyse_vs_tsx_canada_day():
    # 2026-07-01 (Wed) = Canada Day: TSX closed, NYSE open → exchange-aware, not naive "yesterday".
    now = datetime(2026, 7, 1, 21, 0, tzinfo=UTC)
    assert last_completed_session(now, "NYSE") == date(2026, 7, 1)
    assert last_completed_session(now, "TSX") == date(2026, 6, 30)


def test_overnight_gap():
    assert overnight_gap(110.0, 100.0, 3.0)[2] is True      # gap 10 > 2*3
    assert overnight_gap(104.0, 100.0, 3.0)[2] is False     # gap 4 < 6
    g, pct, _ = overnight_gap(110.0, 100.0, 3.0)
    assert abs(g - 10.0) < 1e-9 and abs(pct - 0.10) < 1e-9
    assert overnight_gap(None, 100.0, 3.0)[2] is False      # missing live → fail-open
    assert overnight_gap(110.0, 100.0, 0.0)[2] is False     # atr 0 → fail-open
