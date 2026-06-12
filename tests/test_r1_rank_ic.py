"""Pure-math helpers of scripts/r1_rank_ic.py (synthetic data, no I/O).

Locks the causality contract (only outcomes EXITED before a candidate's entry
may feed its features), the ATR/RS as-of convention (last bar strictly before
the T+1-open fill date), and the replay's top-K accounting.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from r1_rank_ic import (  # noqa: E402
    atr14_before,
    bind_scores,
    causal_group_mean,
    composite_score,
    rank_ic,
    replay_fill,
    return_20d_before,
)


def _d(s: str) -> pd.Timestamp:
    return pd.Timestamp(s)


# ── causal_group_mean ─────────────────────────────────────────────────────────

def test_causal_mean_only_counts_exited_before_entry():
    df = pd.DataFrame({
        "ticker": ["A", "A", "A"],
        "entry_date": [_d("2020-01-01"), _d("2020-02-01"), _d("2020-03-01")],
        # first trade exits AFTER the second one enters — must not leak into it
        "exit_date": [_d("2020-02-15"), _d("2020-02-20"), _d("2020-03-10")],
        "eff_r": [1.0, -0.5, 0.0],
    })
    out = causal_group_mean(df, "ticker", window_days=90)
    assert np.isnan(out[0])                 # nothing exited yet
    assert np.isnan(out[1])                 # trade 0 still open on 02-01
    assert out[2] == pytest.approx((1.0 - 0.5) / 2)   # both exited by 03-01


def test_causal_mean_window_cutoff():
    df = pd.DataFrame({
        "ticker": ["A", "A"],
        "entry_date": [_d("2020-01-10"), _d("2020-06-01")],
        "exit_date": [_d("2020-01-20"), _d("2020-06-05")],
        "eff_r": [2.0, 0.0],
    })
    # exit 2020-01-20 is >90d before entry 2020-06-01 — outside the window
    out = causal_group_mean(df, "ticker", window_days=90)
    assert np.isnan(out[1])
    # without a window it counts
    out_all = causal_group_mean(df, "ticker", window_days=None)
    assert out_all[1] == pytest.approx(2.0)


def test_causal_mean_min_prior():
    df = pd.DataFrame({
        "regime": ["BULL"] * 3,
        "entry_date": [_d("2020-01-01"), _d("2020-02-01"), _d("2020-03-01")],
        "exit_date": [_d("2020-01-05"), _d("2020-02-05"), _d("2020-03-05")],
        "eff_r": [1.0, 1.0, 1.0],
    })
    out = causal_group_mean(df, "regime", min_prior=2)
    assert np.isnan(out[0]) and np.isnan(out[1])      # 0 and 1 priors
    assert out[2] == pytest.approx(1.0)               # 2 priors


# ── as-of price helpers ───────────────────────────────────────────────────────

def _px(n=30, start="2020-01-01", tr=1.0):
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series(100.0, index=idx)
    return pd.DataFrame({"high": close + tr / 2, "low": close - tr / 2,
                         "close": close}, index=idx)


def test_atr14_constant_tr():
    df = _px(30, tr=2.0)
    when = df.index[-1] + pd.Timedelta(days=1)
    assert atr14_before(df, when) == pytest.approx(2.0, rel=1e-6)


def test_atr14_excludes_the_fill_bar():
    df = _px(30, tr=2.0)
    # poison the LAST bar; as-of that bar's date it must be excluded
    df.loc[df.index[-1], ["high", "low"]] = [200.0, 50.0]
    assert atr14_before(df, df.index[-1]) == pytest.approx(2.0, rel=1e-6)


def test_atr14_insufficient_history():
    assert np.isnan(atr14_before(_px(10), _px(10).index[-1]))


def test_return_20d_before():
    idx = pd.bdate_range("2020-01-01", periods=40)
    close = pd.Series(np.linspace(100, 139, 40), index=idx)
    when = idx[30]
    sub = close.loc[: when - pd.Timedelta(days=1)]
    expected = sub.iloc[-1] / sub.iloc[-21] - 1.0
    assert return_20d_before(close, when) == pytest.approx(expected)
    assert np.isnan(return_20d_before(close.iloc[:15], idx[14]))


# ── IC / composite / replay ───────────────────────────────────────────────────

def test_rank_ic_monotone_and_nan_handling():
    f = np.array([1.0, 2.0, 3.0, 4.0, np.nan])
    t = np.array([0.1, 0.2, 0.3, 0.4, 9.9])
    ic, tstat, n = rank_ic(f, t)
    assert ic == pytest.approx(1.0)
    assert n == 4                                   # NaN pair dropped


def test_composite_inverts_and_neutralizes():
    feats = pd.DataFrame({
        "a": [1.0, 2.0, 3.0],
        "b": [3.0, 2.0, 1.0],
    })
    comp = composite_score(feats, invert=("b",))
    # b inverted == a's ordering → strictly increasing composite
    assert comp.iloc[0] < comp.iloc[1] < comp.iloc[2]
    all_nan = pd.DataFrame({"a": [np.nan], "b": [np.nan]})
    assert composite_score(all_nan, invert=()).iloc[0] == pytest.approx(0.5)


def test_replay_picks_top_k_by_score():
    d = _d("2021-05-03")
    bind = pd.DataFrame({
        "date": [d] * 4,
        "ticker": list("WXYZ"),
        "source": ["fill", "fill", "capped", "capped"],
        "eff_r": [0.1, -0.2, 1.0, -1.0],
    })
    # score prefers Y (the good capped) and W; K=2
    score = pd.Series([0.9, 0.1, 1.0, 0.0], index=bind.index)
    res = replay_fill(bind, score)
    assert res["days"] == 1
    assert res["actual"] == pytest.approx(-0.1)     # 0.1 - 0.2
    assert res["feature"] == pytest.approx(1.1)     # Y + W
    assert res["oracle"] == pytest.approx(1.1)      # top-2 by eff_r: 1.0 + 0.1


def test_bind_scores_aligns_and_neutralizes_unmatched():
    cand = pd.DataFrame({
        "ticker": ["A", "B"],
        "entry_date": [_d("2021-01-04"), _d("2021-01-04")],
    })
    bind = pd.DataFrame({
        "ticker": ["A", "B", "C"],          # C has no candidate row
        "date": [_d("2021-01-04")] * 3,
    })
    scores = pd.Series([0.9, 0.2], index=cand.index)
    aligned, n_miss = bind_scores(scores, cand, bind)
    assert aligned.tolist() == pytest.approx([0.9, 0.2, 0.5])
    assert n_miss == 1


def test_replay_skips_uncontested_days():
    d = _d("2021-05-03")
    bind = pd.DataFrame({
        "date": [d, d],
        "ticker": ["A", "B"],
        "source": ["fill", "fill"],
        "eff_r": [0.5, 0.5],
    })
    res = replay_fill(bind, pd.Series([1.0, 0.0], index=bind.index))
    assert res["days"] == 0 and res["actual"] == 0.0
