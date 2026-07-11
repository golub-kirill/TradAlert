"""Advisor base-rate table — graded fallback lookup + fail-open load + format."""

from __future__ import annotations

from core.advisor import base_rates

_TABLE = {
    "momentum|BULL_NORMAL|UPTREND": {"n": 50, "win_rate": 0.60, "avg_r": 0.40},
    "momentum|BULL_NORMAL": {"n": 200, "win_rate": 0.55, "avg_r": 0.35},
    "momentum": {"n": 500, "win_rate": 0.50, "avg_r": 0.30},
    "__all__": {"n": 1000, "win_rate": 0.48, "avg_r": 0.25},
}


def test_lookup_most_specific_cell():
    c = base_rates.lookup(_TABLE, "momentum", "BULL_NORMAL", "UPTREND")
    assert c["key"] == "momentum|BULL_NORMAL|UPTREND" and c["win_rate"] == 0.60


def test_lookup_falls_back_when_cell_thin():
    table = dict(_TABLE)
    table["momentum|BULL_NORMAL|UPTREND"] = {"n": 3, "win_rate": 0.9, "avg_r": 1.0}
    c = base_rates.lookup(table, "momentum", "BULL_NORMAL", "UPTREND")
    assert c["key"] == "momentum|BULL_NORMAL"  # thin trend cell skipped


def test_lookup_falls_back_to_signal_type():
    c = base_rates.lookup(_TABLE, "momentum", "BEAR_HIGH", "DOWNTREND")
    assert c["key"] == "momentum"  # no bear cells


def test_lookup_falls_back_to_global():
    c = base_rates.lookup(_TABLE, "unknown_setup", "X", "Y")
    assert c["key"] == "__all__"


def test_lookup_empty_table():
    assert base_rates.lookup({}, "momentum", "BULL", "UP") == {}


def test_format_base_rate():
    s = base_rates.format_base_rate({"n": 50, "win_rate": 0.60, "avg_r": 0.40})
    assert "60% win" in s and "+0.40R" in s and "n=50" in s
    assert base_rates.format_base_rate({}) == ""


def test_load_base_rates_missing_is_empty(tmp_path):
    assert base_rates.load_base_rates(tmp_path / "nope.json") == {}
