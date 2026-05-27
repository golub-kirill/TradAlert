"""
Tests for ``apply_stop_fill`` and ``apply_target_fill`` in ``backtest.backtester``.

The fill models encode the asymmetric reality of gap-through bars: a stop
fills at the worse of (stop, bar_open); a target fills at the better of
(target, bar_open). Both are pure functions, so testing is short.

Run with::

    pytest tests/test_fill_models.py -v
"""

from __future__ import annotations

import pytest

from backtest.backtester import (
    adjust_target_for_slippage,
    apply_stop_fill,
    apply_target_fill,
)


# ─── apply_stop_fill ─────────────────────────────────────────────────────────


def test_stop_intraday_trigger_fills_at_stop():
    """Bar opens above stop, dips below → stop-market fills at stop level."""
    assert apply_stop_fill(initial_stop=100.0, bar_open=102.0) == 100.0


def test_stop_gap_down_fills_at_open():
    """Bar gaps down through stop → fill is worse than stop, at bar_open."""
    assert apply_stop_fill(initial_stop=100.0, bar_open=97.0) == 97.0


def test_stop_open_at_stop_fills_at_stop():
    """Edge case: open exactly at stop. Result is the same either branch."""
    assert apply_stop_fill(initial_stop=100.0, bar_open=100.0) == 100.0


# ─── apply_target_fill ───────────────────────────────────────────────────────


def test_target_intraday_trigger_fills_at_target():
    """Bar opens below target, rallies through → limit-sell fills at target."""
    assert apply_target_fill(initial_target=120.0, bar_open=115.0) == 120.0


def test_target_gap_up_fills_at_open():
    """Bar gaps up through target → fill is *better* than target, at bar_open.

    Without this, the realized R-multiple on gap-up wins is silently
    truncated to the configured R:R — biasing the strategy's reported
    edge against the loss side which already uses the gap model.
    """
    assert apply_target_fill(initial_target=120.0, bar_open=125.0) == 125.0


def test_target_open_at_target_fills_at_target():
    assert apply_target_fill(initial_target=120.0, bar_open=120.0) == 120.0


# ─── symmetry property ───────────────────────────────────────────────────────


@pytest.mark.parametrize("stop,target,bar_open", [
    (100.0, 120.0, 102.0),  # in-range bar — both functions return the level
    (100.0, 120.0, 97.0),  # gap-down through stop
    (100.0, 120.0, 125.0),  # gap-up through target
])
def test_fill_directionality(stop, target, bar_open):
    """Stop-fill never better than stop; target-fill never worse than target."""
    sf = apply_stop_fill(stop, bar_open)
    tf = apply_target_fill(target, bar_open)
    assert sf <= stop, f"stop fill {sf} should be <= stop {stop}"
    assert tf >= target, f"target fill {tf} should be >= target {target}"


# ─── adjust_target_for_slippage ──────────────────────────────────────────────


def test_adjust_target_zero_slippage_returns_configured():
    """No slippage: slipped_entry == close, helper returns the same target the
    FilterEngine computed. Realised R on a clean fill is min_rr exactly."""
    close = 100.0
    stop = 95.0  # risk = $5
    min_rr = 2.5
    configured_target = close + (close - stop) * min_rr  # 112.5
    adj = adjust_target_for_slippage(close, stop, configured_target, min_rr)
    assert adj == pytest.approx(112.5)


def test_adjust_target_with_slippage_yields_exact_min_rr():
    """Realised R on target hit equals min_rr from the slipped entry."""
    close = 100.0
    stop = 95.0
    min_rr = 2.5
    configured_target = 112.5
    slipped_entry = close * 1.001  # 0.1% entry slippage
    adj = adjust_target_for_slippage(slipped_entry, stop, configured_target, min_rr)

    realised_risk = slipped_entry - stop
    realised_r = (adj - slipped_entry) / realised_risk
    assert realised_r == pytest.approx(min_rr)


def test_adjust_target_without_slippage_no_change_in_r():
    """Sanity: with no slippage, configured target already produces min_rr."""
    close = 100.0
    stop = 95.0
    min_rr = 2.5
    configured_target = 112.5
    realised_r = (configured_target - close) / (close - stop)
    assert realised_r == pytest.approx(min_rr)


def test_adjust_target_exit_signal_returns_unchanged():
    """min_rr == 0 indicates an exit (or fallback) — helper is a no-op."""
    assert adjust_target_for_slippage(100.1, 95.0, 112.5, 0.0) == 112.5


def test_adjust_target_negative_min_rr_returns_unchanged():
    """Defensive: negative min_rr should not produce a target below entry."""
    assert adjust_target_for_slippage(100.1, 95.0, 112.5, -1.0) == 112.5


def test_adjust_target_degenerate_stop_above_entry_returns_unchanged():
    """Stop above entry → risk is non-positive — fall back to configured.

    Trade.compute_r already short-circuits to r=0 in this case, so the
    helper's job is to not crash and not produce a worse target.
    """
    assert adjust_target_for_slippage(95.0, 100.0, 105.0, 2.5) == 105.0
    assert adjust_target_for_slippage(95.0, 95.0, 105.0, 2.5) == 105.0


def test_adjust_target_drift_is_at_least_min_rr_slippage_cost():
    """The bug: pre-slippage target understates realised R by slippage cost.

    With 0.1% slippage and ~5% risk, the pre-slippage target yields
    ~min_rr - 0.07 instead of min_rr. The adjusted target yields min_rr.
    """
    close = 100.0
    stop = 95.0
    min_rr = 2.5
    configured_target = 112.5
    slipped_entry = close * 1.001

    realised_pre_fix = (configured_target - slipped_entry) / (slipped_entry - stop)
    adj = adjust_target_for_slippage(slipped_entry, stop, configured_target, min_rr)
    realised_after = (adj - slipped_entry) / (slipped_entry - stop)

    assert realised_pre_fix < min_rr
    assert realised_after == pytest.approx(min_rr)
    drift = min_rr - realised_pre_fix
    assert 0.05 < drift < 0.10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
