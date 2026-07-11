"""Advisor reflection — fail-open load + calibration-line formatting."""

from __future__ import annotations

from core.advisor import reflection


def test_load_missing_is_empty(tmp_path):
    assert reflection.load_reflection(tmp_path / "none.json") == {}


def test_format_empty_table():
    assert reflection.format_reflection({}) == ""


def test_format_thin_returns_empty():
    thin = {"n": 5, "by_verdict": {"agree": {"n": 5, "correct": 0.6, "avg_r": 0.2}}}
    assert reflection.format_reflection(thin) == ""


def test_format_full_line():
    table = {"n": 40, "by_verdict": {
        "agree": {"n": 25, "correct": 0.60, "avg_r": 0.30},
        "disagree": {"n": 15, "correct": 0.40, "avg_r": 0.12},
    }}
    s = reflection.format_reflection(table)
    assert "40 resolved" in s
    assert "agree 60% right (n=25" in s and "+0.30R" in s
    assert "disagree 40% right (n=15" in s and "+0.12R" in s
