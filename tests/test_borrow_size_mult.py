"""
Borrow drag must scale with position size (audit D2): a reduced-size short
borrows proportionally fewer shares, so effective_r = (r_multiple - borrow_drag_r)
* size_mult, not r_multiple*size_mult - borrow_drag_r (which over-charged borrow
on size-reduced shorts). Long-only behavior is unchanged (drag = 0).
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.trade import Trade


def _short(r: float, rate: float, bars: int, size_mult: float) -> Trade:
    t = Trade(
        ticker="X", signal_type="momentum", direction="short",
        entry_date=date(2024, 1, 1), entry_price=100.0,
        initial_stop=102.0, initial_target=94.0,
        exit_date=date(2024, 1, 8), exit_price=94.0, exit_reason="target",
        bars_held=bars, size_mult=size_mult, borrow_annual_rate=rate,
    )
    t.r_multiple = r
    return t


def test_borrow_drag_scales_with_size_mult():
    full = _short(2.0, 0.0252, bars=20, size_mult=1.0)
    half = _short(2.0, 0.0252, bars=20, size_mult=0.5)
    drag = full.borrow_drag_r()
    assert drag > 0

    assert full.effective_r == pytest.approx((2.0 - drag) * 1.0)
    assert half.effective_r == pytest.approx((2.0 - drag) * 0.5)
    # The half-size short pays half the borrow — not the full drag the old
    # r*size_mult - drag formula charged.
    assert half.effective_r != pytest.approx(2.0 * 0.5 - drag)


def test_long_effective_r_unchanged():
    t = Trade(
        ticker="X", signal_type="momentum", direction="long",
        entry_date=date(2024, 1, 1), entry_price=100.0,
        initial_stop=98.0, initial_target=104.0,
        exit_date=date(2024, 1, 8), exit_price=104.0, exit_reason="target",
        bars_held=5, size_mult=0.5,
    )
    t.r_multiple = 2.0
    assert t.effective_r == pytest.approx(1.0)  # (2.0 - 0) * 0.5
