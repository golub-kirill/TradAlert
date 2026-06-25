"""
Survey/report behavioral feeds (COT) must be release-aligned before as_of slicing,
so a print cannot influence a decision made before it was published (audit F2/F4, B2):
COT +3 days (Tuesday report -> Friday release). (NAAIM purged 2026-06-21; AAII earlier.)
"""

from __future__ import annotations

import pandas as pd

from core.behavioral import _RELEASE_LAG_DAYS, _release_align


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


def test_release_lags_are_correct():
    assert _RELEASE_LAG_DAYS["cot_es"] == 3
    assert "naaim" not in _RELEASE_LAG_DAYS   # NAAIM purged 2026-06-21 (COT-only positioning)
    assert "aaii" not in _RELEASE_LAG_DAYS    # sentiment axis purged (no AAII / F&G)
