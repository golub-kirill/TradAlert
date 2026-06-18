"""
Walk-forward IS selection must never tune on a zero-trade/failed config. When a
whole window's sweep workers crash, every point is a zeroed _empty point;
selecting one would corrupt the OOS leg. Selection degrades through:
floor-clearers -> any traded combo -> baseline.
"""

from __future__ import annotations

from types import SimpleNamespace

from backtest.walk_forward import WalkForwardEngine


def _pt(trades: int, er: float, baseline: bool = False):
    return SimpleNamespace(
        stats=SimpleNamespace(trades_count=trades, expectancy_r=er),
        is_baseline=baseline,
    )


def test_prefers_highest_er_among_floor_clearers():
    points = [_pt(25, 0.1, baseline=True), _pt(30, 0.30), _pt(5, 0.90)]
    best = WalkForwardEngine._select_best_is(points, min_trades=20)
    assert best.stats.expectancy_r == 0.30  # the 5-trade 0.90 fluke is below the floor


def test_falls_back_to_any_traded_when_none_clear_floor():
    points = [_pt(0, 0.0, baseline=True), _pt(5, 0.30), _pt(3, 0.90)]
    best = WalkForwardEngine._select_best_is(points, min_trades=20)
    assert best.stats.expectancy_r == 0.90  # best among combos that actually traded


def test_all_failed_window_returns_baseline_not_arbitrary_zero():
    # Every combo zeroed (crashed/_empty). Must return the baseline, not the
    # arbitrary highest-E[R] zero-trade point.
    points = [_pt(0, 0.0, baseline=True), _pt(0, 0.50), _pt(0, 0.90)]
    best = WalkForwardEngine._select_best_is(points, min_trades=20)
    assert best.is_baseline is True


def test_empty_points_returns_none():
    assert WalkForwardEngine._select_best_is([], min_trades=20) is None
