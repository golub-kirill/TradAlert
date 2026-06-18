"""
Exit-quality instrumentation (exit-logic Phase 0): MFE/MAE in R, computed against
the INITIAL-stop denominator (so they agree with r_multiple and never move under a
future dynamic stop), look-ahead-free, MFE >= 0 / MAE <= 0.
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.trade import Trade


def _long(entry: float = 100.0, stop: float = 90.0) -> Trade:  # risk = 10
    return Trade(
        ticker="TEST.1", signal_type="momentum", direction="long",
        entry_date=date(2024, 1, 1), entry_price=entry, initial_stop=stop,
        initial_target=130.0,
    )


def test_mfe_mae_long_hand_computed():
    t = _long()  # entry 100, stop 90, risk 10
    t.update_excursion(105.0, 99.0)
    t.update_excursion(112.0, 96.0)   # peak high 112, low 96
    t.update_excursion(108.0, 102.0)
    t.exit_date = date(2024, 1, 10)
    t.exit_price = 108.0
    t.r_multiple = t.compute_r()      # (108-100)/10 = 0.8
    t.compute_excursion_r()

    assert t.highest_high == 112.0 and t.lowest_low == 96.0
    assert t.mfe_r == pytest.approx(1.2)             # (112-100)/10
    assert t.mae_r == pytest.approx(-0.4)            # (96-100)/10
    assert t.exit_vs_mfe == pytest.approx(0.8 / 1.2)  # captured 2/3 of the peak


def test_mfe_mae_short_mirrors():
    t = Trade(
        ticker="TEST.2", signal_type="momentum", direction="short",
        entry_date=date(2024, 1, 1), entry_price=100.0, initial_stop=110.0,  # risk 10
        initial_target=80.0,
    )
    t.update_excursion(103.0, 90.0)
    t.update_excursion(105.0, 88.0)   # peak high 105, low 88
    t.exit_date = date(2024, 1, 10)
    t.exit_price = 92.0
    t.r_multiple = t.compute_r()      # short: (100-92)/10 = 0.8
    t.compute_excursion_r()

    assert t.mfe_r == pytest.approx(1.2)   # favorable = lowest_low 88 -> (100-88)/10
    assert t.mae_r == pytest.approx(-0.5)  # adverse = highest_high 105 -> (100-105)/10


def test_mfe_clamped_when_never_favorable():
    t = _long()  # entry 100, stop 90
    t.update_excursion(99.0, 95.0)    # high 99 < entry -> no favorable excursion
    t.exit_date = date(2024, 1, 3)
    t.exit_price = 96.0
    t.r_multiple = t.compute_r()
    t.compute_excursion_r()

    assert t.mfe_r == 0.0                   # clamped (never rose above entry)
    assert t.mae_r == pytest.approx(-0.5)   # (95-100)/10
    assert t.exit_vs_mfe is None            # mfe_r <= 0


def test_compute_excursion_noop_when_no_bars_seen():
    t = _long()
    t.exit_date = date(2024, 1, 2)
    t.exit_price = 95.0
    t.r_multiple = t.compute_r()
    t.compute_excursion_r()
    assert t.mfe_r == 0.0 and t.mae_r == 0.0 and t.exit_vs_mfe is None


def _closed(reason, r, mfe, mae):
    t = Trade(
        ticker="TEST.1", signal_type="momentum", direction="long",
        entry_date=date(2024, 1, 1), entry_price=100.0, initial_stop=90.0,
        initial_target=130.0, exit_date=date(2024, 1, 5),
        exit_price=100.0 + r * 10, exit_reason=reason,
    )
    t.r_multiple = r
    t.mfe_r = mfe
    t.mae_r = mae
    return t


def test_exit_quality_by_reason_capture_and_giveback():
    from backtest.stats import exit_quality_by_reason

    trades = [
        _closed("target", 2.0, 2.0, -0.3),
        _closed("target", 2.0, 2.5, -0.2),
        _closed("stop", -1.0, 0.4, -1.0),
        _closed("time_stop", 0.1, 1.5, -0.4),  # hit +1.5R, closed +0.1R -> gave back
    ]
    rows = {q.exit_reason: q for q in exit_quality_by_reason(trades)}

    assert rows["target"].n == 2
    assert rows["target"].avg_mfe_r == pytest.approx(2.25)
    assert rows["target"].capture == pytest.approx(2.0 / 2.25)
    assert rows["time_stop"].pct_gave_back == pytest.approx(1.0)
    assert rows["stop"].pct_gave_back == 0.0  # MFE 0.4 < 1R -> not a give-back

