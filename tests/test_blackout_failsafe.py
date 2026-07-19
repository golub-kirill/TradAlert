"""Macro blackout fail-safe (``macro.blackout_failsafe``, opt-in).

Total macro data loss (every axis missing) historically produced risk_on 0.5 —
a neutral MID-SIZED position on zero information. The opt-in fail-safe sizes it
at the floor instead. Default OFF must stay byte-identical.
"""

from __future__ import annotations

from core.macro import classify_macro_state


def _floor_ceiling(settings):
    m = settings.get("macro", {})
    return float(m.get("size_mult_floor", 0.25)), float(m.get("size_mult_ceiling", 1.0))


def test_blackout_default_rides_the_placeholder_axis():
    """Pins the defect the lever exists for: with EVERY data feed down, the
    hardcoded earnings_breadth placeholder still counts as present, so the
    default sizes well ABOVE neutral — a blackout masquerading as a read."""
    settings = {"macro": {"size_mult_floor": 0.25, "size_mult_ceiling": 1.0}}
    st = classify_macro_state({}, settings=settings)
    floor, ceiling = _floor_ceiling(settings)
    assert st.confidence < 0.2                       # 8 of 9 axes missing
    assert st.size_multiplier > floor + (ceiling - floor) * 0.5


def test_blackout_failsafe_sizes_at_the_floor():
    settings = {"macro": {"size_mult_floor": 0.25, "size_mult_ceiling": 1.0,
                          "blackout_failsafe": True}}
    st = classify_macro_state({}, settings=settings)
    assert st.risk_on_score == 0.0
    floor, _ = _floor_ceiling(settings)
    assert st.size_multiplier == floor


def test_failsafe_inert_when_any_axis_present():
    """The fail-safe touches ONLY the total-blackout branch — partial data keeps
    the renormalized composite untouched (flag on == flag off)."""
    import pandas as pd
    idx = pd.date_range("2020-01-01", periods=400, freq="D")
    series = {  # a real curve axis: DGS10 − DGS3MO = 1.0 → curve_state NORMAL
        "DGS10": pd.DataFrame({"value": [4.0] * 400}, index=idx),
        "DGS3MO": pd.DataFrame({"value": [3.0] * 400}, index=idx),
    }
    base = classify_macro_state(series, settings={"macro": {}})
    assert "curve_state" not in base.missing_axes    # fixture premise: not a blackout
    on = classify_macro_state(series, settings={"macro": {"blackout_failsafe": True}})
    assert on.risk_on_score == base.risk_on_score
    assert on.size_multiplier == base.size_multiplier
