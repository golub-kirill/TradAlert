"""
Sharpe / Sortino must be computed on a CONTIGUOUS monthly series (zero-filled
for calendar months with no closed trade). Aggregating only trade-months drops
the flat months, which silently inflates the annualized ratio and breaks the
sqrt(12) per-month annualization assumption (audit F5).
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.equity_curve import build_curve
from backtest.stats_utils import sharpe_ratio, sortino_ratio
from backtest.trade import Trade


def _trade(exit_d: date, r: float) -> Trade:
    t = Trade(
        ticker="TEST.1", signal_type="momentum", direction="long",
        entry_date=exit_d, entry_price=100.0, initial_stop=90.0,
        initial_target=130.0,
    )
    t.exit_date = exit_d
    t.exit_price = 100.0
    t.r_multiple = r
    return t


def test_sharpe_sortino_use_contiguous_zero_filled_months():
    trades = [
        _trade(date(2024, 1, 15), 1.0),
        _trade(date(2024, 1, 20), 0.5),   # Jan total +1.5
        _trade(date(2024, 6, 10), -0.5),  # Jun total -0.5; Feb-May have no trades
    ]
    ec = build_curve(trades)

    gapped = [1.5, -0.5]
    contiguous = [1.5, 0.0, 0.0, 0.0, 0.0, -0.5]  # Jan..Jun

    # The ratios must reflect the flat months, not just the two active months.
    assert ec.sharpe == pytest.approx(sharpe_ratio(contiguous))
    assert ec.sharpe != pytest.approx(sharpe_ratio(gapped))
    assert ec.sortino == pytest.approx(sortino_ratio(contiguous))

    # The display series still attributes only the two real trade-months.
    assert list(ec.monthly.index) == ["2024-01", "2024-06"]
    assert ec.monthly.loc["2024-01"] == pytest.approx(1.5)
    assert ec.monthly.loc["2024-06"] == pytest.approx(-0.5)
