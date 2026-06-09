"""
Monthly FRED series (CPI/PCE/FEDFUNDS) are dated at the reference month start but
published the following month. The macro classifier must not see an unpublished
print at a past as_of (audit F1, B1): a monthly figure for month M is treated as
known only from the start of M+2.
"""

from __future__ import annotations

import pandas as pd

from core.macro.regime import _MONTHLY_FRED, classify_macro_state


def test_fedfunds_print_invisible_until_release():
    idx = pd.date_range("2024-01-01", periods=24, freq="MS")  # month starts
    vals = [5.0] * 24
    vals[-1] = 6.0  # a +1.0 hike in the final month (2025-12)
    ff = pd.DataFrame({"value": vals}, index=idx)

    # Pre-release: mid-December. The December print (a monthly figure released
    # the following month) is NOT yet visible, so the 6-month policy delta is
    # flat -> HOLD. Without the release lag the December jump would already read
    # as HIKING here.
    pre = classify_macro_state({"FEDFUNDS": ff}, as_of=pd.Timestamp("2025-12-15"))
    assert pre.policy_stance_us == "HOLD"

    # After the conservative release date (month start + 2 months) the hike is
    # visible -> HIKING.
    post = classify_macro_state({"FEDFUNDS": ff}, as_of=pd.Timestamp("2026-02-15"))
    assert post.policy_stance_us == "HIKING"


def test_monthly_fred_set_covers_cpi_pce_fedfunds():
    assert {"PCEPILFE", "CPIAUCSL", "FEDFUNDS"} <= set(_MONTHLY_FRED)


def test_daily_series_unaffected_by_monthly_lag():
    # A daily series (DGS10) is not in _MONTHLY_FRED, so its as_of cutoff is
    # unchanged — the latest value through as_of is used.
    idx = pd.date_range("2025-11-01", periods=40, freq="D")
    dgs10 = pd.DataFrame({"value": [4.0] * 39 + [4.5]}, index=idx)
    dgs3mo = pd.DataFrame({"value": [4.2] * 40}, index=idx)
    st = classify_macro_state(
        {"DGS10": dgs10, "DGS3MO": dgs3mo}, as_of=pd.Timestamp(idx[-1]),
    )
    # 10y(4.5) - 3m(4.2) = +0.3 spread, just above flat -> not INVERTED.
    assert st.curve_state in {"FLAT", "NORMAL"}
