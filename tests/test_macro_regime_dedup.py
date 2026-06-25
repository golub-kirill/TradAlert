"""A duplicated FRED observation date must not crash the macro classifier.

``classify_macro_state`` builds ``net_liq = walcl.loc[common] - tga.loc[common]
- rrp.loc[common]``. ``common`` (an index intersection) is unique, but
``.loc[common]`` reindexes against each series' OWN index, so a duplicated date
in WALCL/WTREGEN/RRPONTSYD (a revised or double-printed FRED print) resurfaces in
``net_liq``. When that duplicate lands on a 2-/4-month lookback label, the
``net_liq.loc[label]`` lookups return a Series and the ``delta`` truth-tests raise
``ValueError: truth value of a Series is ambiguous``. This locks the de-dup guard.
"""

from __future__ import annotations

import pandas as pd

from core.macro.regime import classify_macro_state


def _frame(index, start: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({"value": [start + i for i in range(len(index))]}, index=index)


def test_duplicate_fred_date_on_lookback_boundary_does_not_crash():
    # 14 monthly observation dates; from the last (2024-02-01) the 4-month
    # lookback label is exactly 2023-10-01. A duplicated WALCL print on that
    # label is what triggered the regime.py:313 Series-truth-value crash.
    base = pd.date_range("2023-01-01", periods=14, freq="MS")  # ... → 2024-02-01
    boundary = pd.Timestamp("2023-10-01")
    assert boundary in base
    walcl_idx = base.insert(list(base).index(boundary) + 1, boundary)  # dup row

    series = {
        "WALCL": _frame(walcl_idx, start=100.0),
        "WTREGEN": _frame(base, start=10.0),
        "RRPONTSYD": _frame(base, start=5.0),
    }

    state = classify_macro_state(series)  # must not raise

    assert state.liquidity_trend in {"EXPANDING", "FLAT", "CONTRACTING"}
    # The axis was actually computed (len(common)=14 ≥ 12), not skipped/defaulted.
    assert "liquidity_trend" not in state.missing_axes
