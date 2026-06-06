"""
Plumbing tests — exercise the sign-of helper, the symmetric
Trade math, the short-side fill helpers, and the direction-aware
adjust_target_for_slippage.

These tests must pass while ``signals.allow_shorts`` is still False —
the plumbing is in place but the backtester does not fire short trades
yet. The goal here is to lock down the math contract so the
integration layer can build on a stable foundation.
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.backtester import (
    apply_stop_fill, apply_target_fill,
    apply_stop_fill_short, apply_target_fill_short,
    adjust_target_for_slippage,
)
from backtest.trade import Trade
from core.types import DIRECTION, sign_of


# ─── sign_of ─────────────────────────────────────────────────────────────────


def test_sign_of_long():
    assert sign_of(DIRECTION.LONG) == 1
    assert sign_of("long") == 1


def test_sign_of_short():
    assert sign_of(DIRECTION.SHORT) == -1
    assert sign_of("short") == -1


def test_sign_of_rejects_bad_input():
    """exit_long / exit_short / none / typos must raise — they have no sign."""
    for bad in ("exit_long", "exit_short", "none", "LONG", "buy", ""):
        with pytest.raises(ValueError):
            sign_of(bad)


# ─── Trade.compute_r — long-side regression ──────────────────────────────────


def _closed_long(entry=100.0, stop=95.0, exit=110.0) -> Trade:
    """Construct a closed long trade for r-math tests."""
    return Trade(
        ticker="ABC", signal_type="momentum", direction="long",
        entry_date=date(2026, 1, 1), entry_price=entry,
        initial_stop=stop, initial_target=110.0,
        exit_date=date(2026, 1, 5), exit_price=exit,
        exit_reason="target",
    )


def test_compute_r_long_at_target():
    """Long: entry 100, stop 95 (risk=5), target hit at 112.5 → r = +2.5."""
    t = _closed_long(entry=100.0, stop=95.0, exit=112.5)
    assert t.compute_r() == pytest.approx(2.5)


def test_compute_r_long_at_stop():
    t = _closed_long(entry=100.0, stop=95.0, exit=95.0)
    assert t.compute_r() == pytest.approx(-1.0)


def test_risk_per_share_long_positive():
    t = _closed_long(entry=100.0, stop=95.0)
    assert t.risk_per_share == pytest.approx(5.0)


# ─── Trade.compute_r — short-side symmetry ───────────────────────────────────


def _closed_short(entry=100.0, stop=105.0, exit=90.0) -> Trade:
    """Construct a closed short trade. Stop sits ABOVE entry."""
    return Trade(
        ticker="ABC", signal_type="momentum", direction="short",
        entry_date=date(2026, 1, 1), entry_price=entry,
        initial_stop=stop, initial_target=90.0,
        exit_date=date(2026, 1, 5), exit_price=exit,
        exit_reason="target",
    )


def test_risk_per_share_short_positive():
    """Short: stop 105, entry 100 → risk per share is +5 (not -5)."""
    t = _closed_short(entry=100.0, stop=105.0)
    assert t.risk_per_share == pytest.approx(5.0)


def test_compute_r_short_at_target():
    """Short: entry 100, stop 105, target 87.5 → r = +2.5 (profitable)."""
    t = _closed_short(entry=100.0, stop=105.0, exit=87.5)
    assert t.compute_r() == pytest.approx(2.5)


def test_compute_r_short_at_stop():
    """Short: stop hit at 105 → r = -1.0."""
    t = _closed_short(entry=100.0, stop=105.0, exit=105.0)
    assert t.compute_r() == pytest.approx(-1.0)


def test_compute_r_short_loss_beyond_stop():
    """Short gap-up beyond stop: bigger loss than -1R."""
    t = _closed_short(entry=100.0, stop=105.0, exit=108.0)
    # risk = 5; loss = entry-exit = 100-108 = -8 → r = -8/5 = -1.6
    assert t.compute_r() == pytest.approx(-1.6)


def test_compute_r_short_winner_below_target():
    """Short closed somewhere between entry and target → small positive r."""
    t = _closed_short(entry=100.0, stop=105.0, exit=97.5)
    # risk = 5; profit = 100-97.5 = 2.5 → r = 0.5
    assert t.compute_r() == pytest.approx(0.5)


def test_long_and_short_symmetric_r_on_target():
    """Long and short with same R-distance to target → same r_multiple."""
    long_t = _closed_long(entry=100.0, stop=95.0, exit=112.5)
    short_t = _closed_short(entry=100.0, stop=105.0, exit=87.5)
    assert long_t.compute_r() == pytest.approx(short_t.compute_r())


def test_long_and_short_symmetric_r_on_stop():
    long_t = _closed_long(entry=100.0, stop=95.0, exit=95.0)
    short_t = _closed_short(entry=100.0, stop=105.0, exit=105.0)
    assert long_t.compute_r() == pytest.approx(short_t.compute_r())


# ─── apply_stop_fill_short / apply_target_fill_short ─────────────────────────


def test_stop_fill_short_intraday():
    """Bar opens below stop, rallies through → buy-to-cover fills at stop."""
    assert apply_stop_fill_short(initial_stop=105.0, bar_open=102.0) == 105.0


def test_stop_fill_short_gap_up():
    """Bar gaps up through stop → fill at bar_open (worse than stop)."""
    assert apply_stop_fill_short(initial_stop=105.0, bar_open=110.0) == 110.0


def test_target_fill_short_intraday():
    """Bar opens above target, drops through → buy-to-cover fills at target."""
    assert apply_target_fill_short(initial_target=90.0, bar_open=95.0) == 90.0


def test_target_fill_short_gap_down():
    """Bar gaps down through target → fill at bar_open (better than target)."""
    assert apply_target_fill_short(initial_target=90.0, bar_open=85.0) == 85.0


@pytest.mark.parametrize("stop,target,bar_open", [
    (105.0, 90.0, 102.0),  # in-range bar
    (105.0, 90.0, 110.0),  # gap-up through stop
    (105.0, 90.0, 85.0),  # gap-down through target
])
def test_short_fill_directionality(stop, target, bar_open):
    """Short stop fill is never below stop; short target fill is never above target."""
    sf = apply_stop_fill_short(stop, bar_open)
    tf = apply_target_fill_short(target, bar_open)
    assert sf >= stop, f"short stop fill {sf} should be >= stop {stop}"
    assert tf <= target, f"short target fill {tf} should be <= target {target}"


# ─── apply_*_fill — long-side regression (must still pass unchanged) ─────────


def test_long_fill_helpers_unchanged():
    """Long helpers behave exactly as before."""
    assert apply_stop_fill(100.0, 102.0) == 100.0  # intraday
    assert apply_stop_fill(100.0, 97.0) == 97.0  # gap-down
    assert apply_target_fill(120.0, 115.0) == 120.0  # intraday
    assert apply_target_fill(120.0, 125.0) == 125.0  # gap-up


# ─── adjust_target_for_slippage — both directions ────────────────────────────


def test_adjust_target_long_default_direction():
    """Default direction='long' preserves long-side semantics."""
    adj = adjust_target_for_slippage(
        entry_price=100.1, initial_stop=95.0,
        configured_target=112.5, min_rr=2.5,
    )
    # risk = 100.1 - 95 = 5.1; target = 100.1 + 5.1 * 2.5 = 112.85
    assert adj == pytest.approx(112.85)


def test_adjust_target_long_explicit_direction():
    """Same result whether direction is implicit (default) or explicit 'long'."""
    a = adjust_target_for_slippage(100.1, 95.0, 112.5, 2.5)
    b = adjust_target_for_slippage(100.1, 95.0, 112.5, 2.5, direction="long")
    assert a == b


def test_adjust_target_short_yields_exact_min_rr():
    """Short: slipped sell-entry 99.9, stop 105 → adjusted target below entry,
    realised R on a target hit equals min_rr."""
    slipped_entry = 99.9
    stop = 105.0
    min_rr = 2.5
    adj = adjust_target_for_slippage(slipped_entry, stop, 87.5, min_rr,
                                     direction="short")
    risk = stop - slipped_entry  # 5.1
    realised_r = (slipped_entry - adj) / risk
    assert realised_r == pytest.approx(min_rr)


def test_adjust_target_short_degenerate_returns_unchanged():
    """Short with stop below entry (pathological) → no adjustment."""
    assert adjust_target_for_slippage(
        entry_price=100.0, initial_stop=95.0,
        configured_target=80.0, min_rr=2.5, direction="short",
    ) == 80.0


def test_adjust_target_min_rr_zero_returns_unchanged_both_directions():
    """min_rr <= 0 → exit signal or fallback; helper is a no-op."""
    assert adjust_target_for_slippage(100.0, 95.0, 110.0, 0.0,
                                      direction="long") == 110.0
    assert adjust_target_for_slippage(100.0, 105.0, 90.0, 0.0,
                                      direction="short") == 90.0


# ─── MarketRegime.allows_shorts ──────────────────────────────────────────────


def test_market_regime_allows_shorts_matrix():
    from core.filter_engine import MarketRegime
    cases = [
        # (trend, volatility) → (allows_longs, allows_shorts)
        ("BULL", "LOW", (True, False)),
        ("BULL", "NORMAL", (True, False)),
        ("BULL", "HIGH", (False, False)),
        ("BEAR", "LOW", (False, True)),
        ("BEAR", "NORMAL", (False, True)),
        ("BEAR", "HIGH", (False, False)),
        ("CHOP", "LOW", (False, False)),
        ("CHOP", "NORMAL", (False, False)),
        ("CHOP", "HIGH", (False, False)),
    ]
    for trend, vol, (exp_long, exp_short) in cases:
        r = MarketRegime(trend=trend, volatility=vol)
        assert r.allows_longs == exp_long, f"{trend}/{vol}: allows_longs"
        assert r.allows_shorts == exp_short, f"{trend}/{vol}: allows_shorts"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
