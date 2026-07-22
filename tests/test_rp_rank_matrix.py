"""
``build_rp_rank_matrix`` must agree with ``build_rp_rank_table``.

The matrix is the vectorised form used to rank the cross-section on every bar of
a backtest; the table is the per-call form the live scanner uses. If they drift,
a factor validated on one is not the factor traded by the other — so the
agreement is asserted directly rather than assumed from shared constants.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators.rp_rank import (
    build_rp_rank_matrix,
    build_rp_rank_table,
    compute_rp_weighted_return,
)


def _prices(n: int, seed: int, start: float = 100.0) -> pd.DataFrame:
    """Deterministic pseudo-random walk on a business-day index."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.015, size=n)
    close = start * np.exp(np.cumsum(steps))
    idx = pd.date_range("2015-01-01", periods=n, freq="B")
    return pd.DataFrame({"close": close}, index=idx)


@pytest.fixture
def universe() -> dict[str, pd.DataFrame]:
    # TEST.* per the project's example-data convention.
    return {f"TEST.{i}": _prices(600, seed=i) for i in range(1, 9)}


def test_matrix_matches_table_on_the_same_date(universe):
    """The whole point: same date, same ranking."""
    mat = build_rp_rank_matrix(universe)
    as_of = mat.index[-1].date()
    table = build_rp_rank_table(universe, as_of=as_of)

    row = mat.loc[mat.index[-1]].dropna()
    assert set(row.index) == set(table), "different tickers ranked"
    for ticker, rank in table.items():
        # build_rp_rank_table rounds to int; the matrix keeps the float.
        assert round(float(row[ticker])) == rank, ticker


def test_matrix_matches_table_on_an_interior_date(universe):
    """Not just the last bar — an interior date must agree too, which is what a
    backtest actually reads."""
    mat = build_rp_rank_matrix(universe)
    ts = mat.index[400]
    table = build_rp_rank_table(universe, as_of=ts.date())

    row = mat.loc[ts].dropna()
    assert set(row.index) == set(table)
    for ticker, rank in table.items():
        assert round(float(row[ticker])) == rank, ticker


def test_underlying_factor_matches_the_scalar_form(universe):
    """Guards the ranking layer from hiding a factor mismatch: check the weighted
    return itself, not only its rank."""
    ticker, df = next(iter(universe.items()))
    scalar = compute_rp_weighted_return(df)

    close = df["close"]
    vec = sum(w * (close / close.shift(m * 21) - 1.0)
              for m, w in {3: 0.4, 6: 0.2, 9: 0.2, 12: 0.2}.items()).iloc[-1]
    assert vec == pytest.approx(scalar, rel=1e-12)


def test_warmup_is_nan_not_zero(universe):
    """A ticker without ~12 months of history has NO rank. Filling it with 0
    would read as 'worst in the cross-section' and silently bias selection."""
    mat = build_rp_rank_matrix(universe)
    assert mat.iloc[:252].isna().all().all()
    assert mat.iloc[-1].notna().all()


def test_ranks_span_the_expected_range(universe):
    mat = build_rp_rank_matrix(universe).dropna(how="all")
    vals = mat.to_numpy(dtype=float)
    vals = vals[~np.isnan(vals)]
    assert vals.min() >= 0.0 and vals.max() <= 99.0


def test_short_ticker_is_excluded_not_crashed():
    """A name with too little history must drop out of the cross-section rather
    than abort the matrix build."""
    uni = {"TEST.1": _prices(600, seed=1),
           "TEST.2": _prices(600, seed=2),
           "TEST.3": _prices(50, seed=3)}          # far short of the 252-bar need
    mat = build_rp_rank_matrix(uni)
    assert mat["TEST.3"].isna().all()
    assert mat.loc[mat.index[-1], ["TEST.1", "TEST.2"]].notna().all()


def test_empty_universe_returns_empty_frame():
    assert build_rp_rank_matrix({}).empty
