"""
Cross-sectional top-K selection (``PortfolioConfig.top_k``).

Turns selection from ABSOLUTE (every name that clears its own gates competes for
the budget in scan order) into RELATIVE (only the K highest-ranked candidates on
a bar may fill). The pre-registered structural lever —
docs/backtest_out/xsec_topk_prereg.md.

The load-bearing invariants here are that OFF is bit-identical, and that the rank
row consulted is the one the engine could actually have seen. Signals fire at a
bar's close and fill at the next bar's open, so selection must read the rank row
STRICTLY BEFORE the fill bar; reading the fill bar's own row would be look-ahead
that no live scan could reproduce.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.portfolio_backtester import PortfolioBacktester, PortfolioConfig
from core.filter_engine import MarketRegime, SignalResult

TICKERS = ["TEST.1", "TEST.2", "TEST.3", "TEST.4"]


class _AllLongEngine:
    """Emit one long per ticker while flat, then hold — so every ticker is a
    candidate on the same bar and they must compete."""

    def __init__(self) -> None:
        self._today = None
        self._emitted: set[str] = set()

    def market_regime(self, market_t, vix_t):
        return MarketRegime(trend="BULL", volatility="LOW")

    def signal(self, ticker, df, *, market_dfs=None, vix_df=None,
               earnings_date=None, held_long=False, held_short=False, regime=None):
        if held_long or held_short or ticker in self._emitted:
            return SignalResult(passed=False, reason="hold")
        self._emitted.add(ticker)
        close = float(df["close"].iloc[-1])
        return SignalResult(
            passed=True, direction="long", signal_type="momentum",
            stop_price=close - 500.0, target_price=close + 500.0, min_rr=1.0,
            size_mult=1.0, market_regime="BULL_LOW", ticker_trend="UPTREND",
            reason="stub: long entry",
        )


def _prepped(n=30, px=100.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
         "volume": 1_000_000.0, "atr": 1.0, "rsi": 45.0,
         "macd": 0.0, "macd_signal": 0.0, "macd_hist": -0.05,
         "ma_fast": px - 2, "ma_slow": px - 5},
        index=idx,
    )
    return {t: SimpleNamespace(df=df.copy(), earnings_history=None) for t in TICKERS}, idx


def _ranks(idx, mapping, n_pad=60):
    """Rank frame over `idx`. `mapping` is ticker -> constant rank. Padding
    columns keep the cross-section above the rank_min_names floor."""
    data = {t: [v] * len(idx) for t, v in mapping.items()}
    for i in range(n_pad):
        data[f"PAD.{i}"] = [1.0] * len(idx)
    return pd.DataFrame(data, index=idx)


def _run(top_k=None, ranks=None, min_names=50, budget=10.0):
    prepped, idx = _prepped()
    cfg = PortfolioConfig(max_open_risk=budget, close_open_at_eod=True,
                          top_k=top_k, rank_matrix=ranks, rank_min_names=min_names)
    return PortfolioBacktester(engine=_AllLongEngine(), cfg=cfg).run_prepped(prepped, {})


# ── OFF is bit-identical ──────────────────────────────────────────────────────

def test_off_by_default_lets_every_candidate_through():
    res = _run(top_k=None)
    assert {t.ticker for t in res.trades} == set(TICKERS)
    assert res.rank_dropped == 0


def test_rank_matrix_without_top_k_changes_nothing():
    """Supplying ranks must not by itself alter behaviour — only top_k activates
    selection."""
    _, idx = _prepped()
    ranks = _ranks(idx, {"TEST.1": 99.0, "TEST.2": 70.0, "TEST.3": 40.0, "TEST.4": 10.0})
    off = _run(top_k=None, ranks=ranks)
    assert {t.ticker for t in off.trades} == set(TICKERS)
    assert off.rank_dropped == 0


# ── selection ─────────────────────────────────────────────────────────────────

def test_keeps_only_the_k_highest_ranked():
    _, idx = _prepped()
    ranks = _ranks(idx, {"TEST.1": 10.0, "TEST.2": 99.0, "TEST.3": 40.0, "TEST.4": 70.0})
    res = _run(top_k=2, ranks=ranks)
    assert {t.ticker for t in res.trades} == {"TEST.2", "TEST.4"}
    assert res.rank_dropped == 2
    assert {c.ticker for c in res.capped_signals} == {"TEST.1", "TEST.3"}


def test_k_at_or_above_candidate_count_is_inert():
    _, idx = _prepped()
    ranks = _ranks(idx, {"TEST.1": 10.0, "TEST.2": 99.0, "TEST.3": 40.0, "TEST.4": 70.0})
    res = _run(top_k=4, ranks=ranks)
    assert {t.ticker for t in res.trades} == set(TICKERS)
    assert res.rank_dropped == 0


# ── look-ahead contract ───────────────────────────────────────────────────────

def test_reads_the_rank_row_strictly_before_the_fill_bar():
    """The decisive test. Ranks are INVERTED on the fill bar relative to the
    signal bar. Selection must use the signal bar's ranks — using the fill bar's
    own row would pick the opposite names, and no live scan could do that."""
    prepped, idx = _prepped()
    # Signal fires at close of idx[0] and fills at open of idx[1].
    early = {"TEST.1": 99.0, "TEST.2": 90.0, "TEST.3": 10.0, "TEST.4": 1.0}
    late = {"TEST.1": 1.0, "TEST.2": 10.0, "TEST.3": 90.0, "TEST.4": 99.0}
    rows = []
    for i, ts in enumerate(idx):
        rows.append(early if i == 0 else late)
    data = {t: [r[t] for r in rows] for t in TICKERS}
    for i in range(60):
        data[f"PAD.{i}"] = [1.0] * len(idx)
    ranks = pd.DataFrame(data, index=idx)

    cfg = PortfolioConfig(max_open_risk=10.0, close_open_at_eod=True,
                          top_k=2, rank_matrix=ranks, rank_min_names=50)
    res = PortfolioBacktester(engine=_AllLongEngine(), cfg=cfg).run_prepped(prepped, {})

    picked = {t.ticker for t in res.trades}
    assert picked == {"TEST.1", "TEST.2"}, (
        f"picked {picked} — selection used the FILL bar's ranks (look-ahead) "
        "instead of the signal bar's")


# ── degradation paths ─────────────────────────────────────────────────────────

def test_thin_cross_section_makes_selection_inert():
    """Below rank_min_names the factor's cross-section is too thin to rank on
    (names still warming up), so top_k must stand down rather than select on
    noise."""
    _, idx = _prepped()
    ranks = _ranks(idx, {"TEST.1": 10.0, "TEST.2": 99.0,
                         "TEST.3": 40.0, "TEST.4": 70.0}, n_pad=0)
    res = _run(top_k=2, ranks=ranks, min_names=50)
    assert {t.ticker for t in res.trades} == set(TICKERS)
    assert res.rank_dropped == 0


def test_missing_rank_matrix_degrades_to_baseline():
    """A misconfigured run must fall back to baseline selection, not rank on
    nothing and not crash."""
    res = _run(top_k=2, ranks=None)
    assert {t.ticker for t in res.trades} == set(TICKERS)
    assert res.rank_dropped == 0


def test_unranked_candidates_lose_to_ranked_ones():
    """A name with no rank (insufficient history) must not outrank a ranked one;
    it is droppable, never preferred."""
    _, idx = _prepped()
    ranks = _ranks(idx, {"TEST.1": 50.0, "TEST.2": 60.0, "TEST.3": 40.0})
    ranks["TEST.4"] = float("nan")          # never warmed up
    res = _run(top_k=2, ranks=ranks)
    assert "TEST.4" not in {t.ticker for t in res.trades}
    assert {t.ticker for t in res.trades} == {"TEST.1", "TEST.2"}
