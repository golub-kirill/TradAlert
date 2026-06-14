"""Gap-through accounting (audit V2): the 0R-by-design convention and the
documented immateriality bound (backtest/trade.py compute_r docstring).

A gap-through entry is a T+1 open that fills at/through the initial stop —
risk_per_share ≤ 0, R undefined. The backtester scores it 0R (the same-bar
stop exits at that same open, so the only real cost is slippage). The second
test re-derives the docstring's measured claim from the frozen-snapshot
baseline dump so the figure can't silently drift as universe/params change.
"""

from datetime import date
from pathlib import Path

import pytest

from backtest.trade import Trade

_DUMP = (Path(__file__).resolve().parents[1]
         / "docs" / "backtest_out" / "studies" / "b3" / "full_0.002.parquet")


def _long(entry: float, stop: float) -> Trade:
    return Trade(
        ticker="TEST.1", signal_type="momentum", direction="long",
        entry_date=date(2024, 1, 1), entry_price=entry, initial_stop=stop,
        initial_target=130.0,
    )


def test_gap_through_scores_zero_r():
    # long fill BELOW the stop: risk_per_share ≤ 0 → 0R, not a fake huge loss
    t = _long(entry=89.0, stop=90.0)
    t.exit_date = date(2024, 1, 2)
    t.exit_price = 89.0
    assert t.risk_per_share <= 0
    assert t.compute_r() == 0.0


def test_normal_entry_still_scores():
    t = _long(entry=100.0, stop=90.0)
    t.exit_date = date(2024, 1, 5)
    t.exit_price = 105.0
    assert t.compute_r() == pytest.approx(0.5)


@pytest.mark.skipif(not _DUMP.exists(), reason="snapshot baseline dump absent")
def test_gap_through_immaterial_in_headline_dump():
    """Docstring drift-guard: gap-throughs stay rare and ~free in the headline.

    Re-measured 2026-06-11: 6 of 1622 (all 2016-06-24), booked −0.005R each
    (commission only). The bounds below are deliberately loose — they catch a
    regime change (gap-throughs becoming common or expensive), not data jitter.
    """
    pd = pytest.importorskip("pandas")
    df = pd.read_parquet(_DUMP)
    gt = df[df["entry_price"] <= df["initial_stop"]]   # long-only universe
    assert len(gt) / len(df) < 0.01                    # < 1% of trades
    # booked cost is commission-only per trade, immaterial in aggregate
    assert (gt["r_multiple"].abs() <= 0.02).all()
    assert gt["r_multiple"].sum() > -0.5
