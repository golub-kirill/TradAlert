"""Unit tests for the FROZEN short-side acceptance bars (scripts/shorts_validate.evaluate_shorts_bars).

Pure verdict-logic checks — no backtest, no I/O — so the gate logic is guarded without
the ~10-min paired snapshot run. Bars are frozen in docs/backtest_out/shorts_validation_prereg.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "studies"))

from shorts_validate import evaluate_shorts_bars  # noqa: E402


def _kw(**over):
    """A baseline kwargs set that PASSES every bar; tests mutate one field to fail one bar."""
    base = dict(
        n_bear_shorts=40,
        sharpe_on=0.30, sharpe_off=0.30, calmar_on=0.20, calmar_off=0.10,
        excess_on_band={0.005: 0.10, 0.010: 0.10, 0.020: 0.10},
        excess_off_band={0.005: 0.00, 0.010: 0.00, 0.020: 0.00},
        bear_windows=[
            dict(year=2008, maxdd_on=1.0, maxdd_off=2.0, excess_on=0.5, excess_off=0.1),
            dict(year=2020, maxdd_on=1.0, maxdd_off=2.0, excess_on=0.5, excess_off=0.1),
            dict(year=2022, maxdd_on=3.0, maxdd_off=2.0, excess_on=0.0, excess_off=0.1),
        ],
        maxdd_on_full=30.0, maxdd_off_full=31.0,
    )
    base.update(over)
    return base


def test_all_bars_pass_ships():
    v = evaluate_shorts_bars(**_kw())
    assert v["ship"] and v["gate"] and v["bar1"] and v["bar2"] and v["bar3"] and v["bar4"]


def test_gate_in_underpowered_blocks_ship():
    v = evaluate_shorts_bars(**_kw(n_bear_shorts=10))
    assert not v["gate"] and not v["ship"]


def test_bar1_sharpe_regression_fails():
    v = evaluate_shorts_bars(**_kw(sharpe_on=0.20))   # 0.20 < 0.30*0.98
    assert not v["bar1"] and not v["ship"]


def test_bar1_calmar_regression_fails():
    v = evaluate_shorts_bars(**_kw(calmar_on=0.05))   # 0.05 < 0.10
    assert not v["bar1"]


def test_bar2_base_below_baseline_fails():
    v = evaluate_shorts_bars(**_kw(excess_on_band={0.005: 0.1, 0.010: -0.1, 0.020: 0.1}))
    assert not v["bar2"]


def test_bar2_band_instability_fails_even_if_base_ok():
    # base (1%) passes but the 0.5% band point drops below baseline → not sign-stable
    v = evaluate_shorts_bars(**_kw(excess_on_band={0.005: -0.1, 0.010: 0.1, 0.020: 0.1}))
    assert not v["bar2"]


def test_bar3_needs_two_of_three_bear_windows():
    bw = [
        dict(year=2008, maxdd_on=1.0, maxdd_off=2.0, excess_on=0.5, excess_off=0.1),  # ok
        dict(year=2020, maxdd_on=3.0, maxdd_off=2.0, excess_on=0.5, excess_off=0.1),  # maxDD worse
        dict(year=2022, maxdd_on=1.0, maxdd_off=2.0, excess_on=0.0, excess_off=0.1),  # excess worse
    ]
    v = evaluate_shorts_bars(**_kw(bear_windows=bw))
    assert v["bear_windows_ok"] == 1 and not v["bar3"]


def test_bar4_drawdown_regression_fails():
    v = evaluate_shorts_bars(**_kw(maxdd_on_full=34.0))   # 34 > 31 + 2
    assert not v["bar4"] and not v["ship"]
