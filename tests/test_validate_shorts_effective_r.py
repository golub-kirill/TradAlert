"""validate_shorts must judge the short side on size- and borrow-adjusted
effective_r, not raw per-unit r_multiple (audit M7).

The economic Sharpe/Calmar check (#4) uses effective_r when the ledger carries it,
falling back to r_multiple for older ledgers.
"""

from __future__ import annotations

import pandas as pd

from backtest.validate_shorts import run_checks


def _ledger(with_effective: bool) -> pd.DataFrame:
    cols = {
        "direction": ["short", "short", "long", "long"],
        "exit_reason": ["target", "stop", "target", "stop"],
        "r_multiple": [2.0, -1.0, 2.0, -1.0],
        "entry_date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]),
        "exit_date": pd.to_datetime(["2024-01-05", "2024-01-06", "2024-01-07", "2024-01-08"]),
    }
    if with_effective:
        # varying size_mult → effective_r is a different distribution, not a pure scale
        cols["effective_r"] = [1.0, -1.0, 0.5, -1.0]
    return pd.DataFrame(cols)


def _check4(df, baseline):
    return next(c for c in run_checks(df, baseline) if c.name.startswith("4."))


def test_check4_uses_effective_r_when_present():
    df = _ledger(with_effective=True)
    c4 = _check4(df, df.copy())
    assert "R=effective_r" in c4.detail


def test_check4_falls_back_to_r_multiple_for_old_ledgers():
    df = _ledger(with_effective=False)
    c4 = _check4(df, df.copy())
    assert "R=r_multiple" in c4.detail
