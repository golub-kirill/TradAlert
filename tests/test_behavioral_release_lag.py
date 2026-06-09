"""
Survey/report behavioral feeds (COT, NAAIM, AAII) must be release-aligned before
as_of slicing, so a print cannot influence a decision made before it was
published (audit F2/F4, B2): COT +3 days (Tuesday report -> Friday release),
NAAIM/AAII +1 day.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.behavioral import (
    _RELEASE_LAG_DAYS,
    _classify_sentiment,
    _release_align,
    classify_behavioral_state,
)


def test_release_align_shifts_index_forward_without_mutating_input():
    idx = pd.to_datetime(["2024-01-02", "2024-01-09"])  # Tuesdays
    df = pd.DataFrame({"lev_net": [1.0, 2.0]}, index=idx)
    out = _release_align(df, 3)
    assert list(out.index) == list(idx + pd.Timedelta(days=3))
    assert list(df.index) == list(idx)  # input untouched


def test_release_align_passthrough_on_non_datetime_or_zero_lag():
    plain = pd.DataFrame({"x": [1]}, index=[0])
    assert _release_align(plain, 3) is plain          # non-datetime index untouched
    dt = pd.DataFrame({"x": [1]}, index=pd.to_datetime(["2024-01-02"]))
    assert _release_align(dt, 0) is dt                # zero lag untouched


def _aaii_with_extreme_last(n: int = 60) -> pd.DataFrame:
    """Weekly AAII spreads: mostly flat (some noise for non-zero std), with an
    extreme final reading that flips sentiment to EUPHORIA when it is visible."""
    idx = pd.date_range("2023-01-04", periods=n, freq="W-WED")  # survey Wednesdays
    spread = [0.0] * n
    spread[10] = 5.0
    spread[20] = -5.0
    spread[-1] = 50.0  # extreme final reading
    return pd.DataFrame({"spread": spread}, index=idx)


def test_aaii_last_survey_invisible_until_release():
    aaii = _aaii_with_extreme_last()
    survey_date = pd.Timestamp(aaii.index[-1])         # Wednesday close
    release_date = survey_date + pd.Timedelta(days=1)  # published Thursday

    # The leak the fix prevents: the OLD code sliced the raw (unaligned) series,
    # which on the survey date already exposes the extreme reading.
    assert _classify_sentiment(aaii.loc[:survey_date]) == "EUPHORIA"

    # Fixed: on the survey date (pre-release) the extreme reading is NOT visible.
    pre = classify_behavioral_state(
        {"aaii": aaii}, settings={}, spy_df=None, as_of=survey_date,
    )
    assert pre.sentiment_state == "NORMAL"

    # From the release date the reading becomes visible -> EUPHORIA.
    post = classify_behavioral_state(
        {"aaii": aaii}, settings={}, spy_df=None, as_of=release_date,
    )
    assert post.sentiment_state == "EUPHORIA"


def test_cot_lag_is_three_days():
    assert _RELEASE_LAG_DAYS["cot_es"] == 3
    assert _RELEASE_LAG_DAYS["naaim"] == 1
    assert _RELEASE_LAG_DAYS["aaii"] == 1
