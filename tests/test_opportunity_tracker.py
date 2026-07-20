"""Pure helpers for scripts/live/opportunity_tracker.py (no DB / no network).

Covers the market-adjusted forward-return geometry, the passed-on row
definition, the reason → gate-family normalizer, the two-sided classifier, and
the per-gate aggregation that feed the opportunity-cost readout. The DB and
price I/O live in main() and are not exercised here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "live"))

import opportunity_tracker as ot  # noqa: E402


def _series(close: np.ndarray, dates: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(close, index=dates).sort_index()


# ── forward_returns ─────────────────────────────────────────────────────────

def test_forward_returns_flat_spy_equals_raw():
    # 40 bars; SPY perfectly flat → market adjustment is a no-op (mkt_adj == raw).
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.linspace(100.0, 139.0, n)          # smooth uptrend
    spy = _series(np.full(n, 500.0), dates)        # flat benchmark
    i0 = 5
    out = ot.forward_returns(close, dates, spy, i0, horizons=(5, 21))
    raw5 = close[i0 + 5] / close[i0] - 1.0
    raw21 = close[i0 + 21] / close[i0] - 1.0
    assert out["fwd5"] == pytest.approx(raw5)
    assert out["fwd21"] == pytest.approx(raw21)


def test_forward_returns_plus_10pct_at_21():
    # Ticker +10% at i0+21 over flat SPY → fwd21 ≈ +0.10.
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    i0 = 2
    close[i0 + 21] = 110.0                          # exactly +10% at the 21-bar mark
    spy = _series(np.full(n, 300.0), dates)         # flat benchmark
    out = ot.forward_returns(close, dates, spy, i0, horizons=(5, 21))
    assert out["fwd21"] == pytest.approx(0.10)


def test_forward_returns_market_adjust_subtracts_spy():
    # Ticker +10% but SPY +4% over the same span → mkt-adj ≈ +6%.
    n = 30
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    spy_vals = np.full(n, 200.0)
    i0 = 1
    close[i0 + 21] = 110.0
    spy_vals[i0 + 21] = 208.0                       # +4% benchmark over the span
    out = ot.forward_returns(close, dates, _series(spy_vals, dates), i0, horizons=(21,))
    assert out["fwd21"] == pytest.approx(0.10 - 0.04)


def test_forward_returns_past_end_is_nan():
    # i0+h runs past the end of the series → NaN for that horizon.
    n = 10
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    spy = _series(np.full(n, 100.0), dates)
    out = ot.forward_returns(close, dates, spy, i0=3, horizons=(5, 21))
    assert out["fwd5"] == pytest.approx(0.0)        # 3+5=8 < 10 → valid
    assert np.isnan(out["fwd21"])                   # 3+21=24 ≥ 10 → NaN


def test_forward_returns_mdd21_when_window_exists():
    # A dip to 90 then recovery inside the 21-window → mdd21 == -0.10.
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    i0 = 2
    close[i0 + 5] = 90.0                             # -10% trough inside the window
    spy = _series(np.full(n, 100.0), dates)
    out = ot.forward_returns(close, dates, spy, i0, horizons=(21,))
    assert out["mdd21"] == pytest.approx(-0.10)


# ── classify ────────────────────────────────────────────────────────────────

def test_classify_missed_winner():
    assert ot.classify(0.08, win=0.05, lose=0.05) == "missed_winner"


def test_classify_avoided_loser():
    assert ot.classify(-0.08, win=0.05, lose=0.05) == "avoided_loser"


def test_classify_neutral():
    assert ot.classify(0.01, win=0.05, lose=0.05) == "neutral"


def test_classify_nan_is_neutral():
    assert ot.classify(float("nan")) == "neutral"


# ── aggregate ───────────────────────────────────────────────────────────────

def test_aggregate_counts_and_mean_sign():
    obs = [
        {"gate": "rsi", "fwd5": 0.02, "fwd21": 0.08, "cls": "missed_winner"},
        {"gate": "rsi", "fwd5": -0.03, "fwd21": -0.10, "cls": "avoided_loser"},
        {"gate": "rsi", "fwd5": 0.0, "fwd21": 0.01, "cls": "neutral"},
        {"gate": "trend", "fwd5": -0.05, "fwd21": -0.12, "cls": "avoided_loser"},
    ]
    out = ot.aggregate(obs)

    rsi = out["rsi"]
    assert rsi["n"] == 3
    assert rsi["pct_missed_winner"] == pytest.approx(100 / 3)
    assert rsi["pct_avoided_loser"] == pytest.approx(100 / 3)
    # mean fwd21 = (0.08 - 0.10 + 0.01)/3 = -0.0033... → negative (avoided losers)
    assert rsi["mean_fwd21"] == pytest.approx((0.08 - 0.10 + 0.01) / 3)
    assert rsi["mean_fwd21"] < 0

    trend = out["trend"]
    assert trend["n"] == 1
    assert trend["pct_avoided_loser"] == pytest.approx(100.0)
    assert trend["mean_fwd21"] == pytest.approx(-0.12)

    allg = out["__ALL__"]
    assert allg["n"] == 4
    assert allg["mean_fwd21"] == pytest.approx((0.08 - 0.10 + 0.01 - 0.12) / 4)
    assert allg["mean_fwd21"] < 0


def test_aggregate_drops_nan_fwd21_from_every_stat():
    # A NaN-inclusive denominator against a NaN-exclusive count understated both
    # percentages — the dropped row must leave the pct denominator too.
    obs = [
        {"gate": "g", "fwd5": 0.01, "fwd21": 0.08, "cls": "missed_winner"},
        {"gate": "g", "fwd5": 0.01, "fwd21": float("nan"), "cls": "neutral"},
    ]
    g = ot.aggregate(obs)["g"]
    assert g["n"] == 1
    assert g["dropped"] == 1
    assert g["mean_fwd21"] == pytest.approx(0.08)
    assert g["pct_missed_winner"] == pytest.approx(100.0)


def test_aggregate_mean_positive_when_cost_you():
    obs = [
        {"gate": "g", "fwd5": 0.05, "fwd21": 0.15, "cls": "missed_winner"},
        {"gate": "g", "fwd5": 0.04, "fwd21": 0.09, "cls": "missed_winner"},
    ]
    out = ot.aggregate(obs)["g"]
    assert out["mean_fwd21"] > 0                    # positive ⇒ the gate cost you
    assert out["pct_missed_winner"] == pytest.approx(100.0)


def test_aggregate_all_dedupes_ticker_month_across_gates():
    # One name blocked by two gates in the same month is one price move, so the
    # ALL rollup must count it once while each gate still sees its own row.
    day = pd.Timestamp("2026-03-04").date()
    obs = [
        {"ticker": "TEST.1", "gate": "a", "signal_date": day,
         "fwd5": 0.0, "fwd21": 0.20, "mdd21": -0.01, "cls": "missed_winner"},
        {"ticker": "TEST.1", "gate": "b", "signal_date": day,
         "fwd5": 0.0, "fwd21": 0.20, "mdd21": -0.01, "cls": "missed_winner"},
        {"ticker": "TEST.2", "gate": "a", "signal_date": day,
         "fwd5": 0.0, "fwd21": -0.10, "mdd21": -0.12, "cls": "avoided_loser"},
    ]
    out = ot.aggregate(obs)
    assert out["a"]["n"] == 2
    assert out["b"]["n"] == 1
    assert out["__ALL__"]["n"] == 2                 # not 3 — TEST.1 counted once
    assert out["__ALL__"]["mean_fwd21"] == pytest.approx((0.20 - 0.10) / 2)


def test_aggregate_reports_median_fwd5_and_mdd():
    obs = [
        {"gate": "g", "fwd5": 0.02, "fwd21": 0.08, "mdd21": -0.04, "cls": "missed_winner"},
        {"gate": "g", "fwd5": 0.04, "fwd21": 0.10, "mdd21": -0.06, "cls": "missed_winner"},
    ]
    g = ot.aggregate(obs)["g"]
    assert g["median_fwd5"] == pytest.approx(0.03)
    assert g["median_mdd21"] == pytest.approx(-0.05)


# ── is_passed_on ────────────────────────────────────────────────────────────

def test_is_passed_on_scan_blocked():
    assert ot.is_passed_on(passed=0, signal_kind="none", declined=0,
                           reason="ATR% 0.29 < min 1.0")


def test_is_passed_on_passed_scan_nothing_fired():
    assert ot.is_passed_on(passed=1, signal_kind="none", declined=0,
                           reason="no entry conditions met")


def test_is_passed_on_excludes_exit_signals():
    # An exit evaluation is a position already held — a forward return there is
    # not an opportunity cost.
    assert not ot.is_passed_on(passed=0, signal_kind="exit_long", declined=0,
                               reason="max-hold reached")
    assert not ot.is_passed_on(passed=1, signal_kind="exit_short", declined=0,
                               reason="stop hit")


def test_is_passed_on_excludes_hold_rows():
    assert not ot.is_passed_on(passed=1, signal_kind="none", declined=0,
                               reason="no exit condition met (hold)")


def test_is_passed_on_declined_always_counts():
    # The owner skipped a FIRED entry — passed-on regardless of signal_kind.
    assert ot.is_passed_on(passed=1, signal_kind="entry_long", declined=1,
                           reason="entry signal fired")


def test_is_passed_on_excludes_live_entry():
    assert not ot.is_passed_on(passed=1, signal_kind="entry_long", declined=0,
                               reason="entry signal fired")


# ── normalize_gate ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason, family", [
    ("ATR% 0.29 < min 1.0", "ATR% < min"),
    ("ATR% 7.10 > max 6.0", "ATR% > max"),
    ("avg dollar vol 1,203,441 < min 5,000,000", "dollar volume < min"),
    ("market cap 42,000,000 < min 300,000,000", "market cap < min"),
    ("price 3.21 < min 5", "price < min"),
    ("no entry conditions met", "no entry conditions met"),
    ("regime CHOP_LOW: trend blocks entries (longs and shorts)", "regime blocks entries"),
    ("earnings in 5d (buffer 5d)", "earnings buffer (pre)"),
    ("earnings 2d ago (two-sided buffer 5d)", "earnings buffer (post)"),
    ("prev bar range 4.12 > 2.0*ATR (1.80)", "gap risk: prev bar range"),
    ("trigger bar red (close 10.10 < open 10.40); anti-gap gate blocks entry",
     "anti-gap: trigger bar red"),
    ("overextended: bb_z 2.61 > 2.00", "overextension veto"),
    ("overextended short: bb_z -2.61 < -2.00", "overextension veto (short)"),
    ("R:R below minimum 1.8", "R:R below minimum"),
    ("only 12 rows — need 200 for scan", "data: insufficient rows"),
    ("no completed sessions after freshness trim", "data: stale/no fresh bar"),
])
def test_normalize_gate_collapses_parameterised_reasons(reason, family):
    assert ot.normalize_gate(reason) == family


def test_normalize_gate_collapses_the_whole_numeric_family_to_one_bucket():
    # The live journal holds ~1.7k distinct reason strings because the numbers
    # are inlined; every ATR% rejection must land in a single bucket.
    variants = [f"ATR% {v:.2f} < min 1.0" for v in (0.11, 0.29, 0.42, 0.99)]
    assert len({ot.normalize_gate(r) for r in variants}) == 1


def test_normalize_gate_scan_pass_snapshot_is_unattributed():
    # filter_engine._scan_pass_reason output: no gate rejected this name, so it
    # must not be reported as a gate that cost you.
    snap = "UPTREND | vol×1.24 | RSI 55.1 | MACD↑"
    assert ot.normalize_gate(snap) == ot.UNATTRIBUTED
    assert ot.normalize_gate("CHOP | vol×0.80 | RSI 41.2 | MACD↓ | 3d✓") == ot.UNATTRIBUTED


def test_normalize_gate_empty_is_unattributed():
    assert ot.normalize_gate(None) == ot.UNATTRIBUTED
    assert ot.normalize_gate("   ") == ot.UNATTRIBUTED


def test_normalize_gate_declined_wins_over_reason():
    assert ot.normalize_gate("no entry conditions met", declined=True) == ot.DECLINED


def test_normalize_gate_unknown_reason_falls_back_to_numeric_template():
    # An unrecognised reason must still collapse by template, or a future engine
    # string re-explodes the cardinality one bucket per observation.
    a = ot.normalize_gate("brand new gate 1.23 vs 4.56")
    b = ot.normalize_gate("brand new gate 9.99 vs 0.01")
    assert a == b
    assert "#" in a


# ── gate_side / short orientation ───────────────────────────────────────────

def test_gate_side_defaults_long():
    assert ot.gate_side("ATR% < min") == "long"


def test_gate_side_short_families():
    assert ot.gate_side("overextension veto (short)") == "short"
    assert ot.gate_side("hard-to-borrow (short blocked)") == "short"


def test_classify_short_side_inverts_sign():
    # A blocked SHORT candidate that fell 8% is a missed winner, not an avoided
    # loser — the long-only classifier had this backwards.
    assert ot.classify(-0.08, side="short") == "missed_winner"
    assert ot.classify(0.08, side="short") == "avoided_loser"


# ── tail statistics ─────────────────────────────────────────────────────────

def test_mean_ex_tail_flips_the_sign_on_a_tail_carried_sample():
    # The live shape in miniature: a net-negative body plus one big winner. The
    # plain mean reads positive; removing the top 5% flips it. When these
    # disagree the headline is a tail artifact, not a population property.
    sample = [-1.0] * 19 + [30.0]
    assert np.mean(sample) > 0
    assert ot.mean_ex_tail(sample, 0.05) == pytest.approx(-1.0)


def test_tail_share_above_one_means_the_body_is_negative():
    sample = [-1.0] * 19 + [30.0]          # total = 11, top 1 = 30
    assert ot.tail_share(sample, 0.05) == pytest.approx(30.0 / 11.0)
    assert ot.tail_share(sample, 0.05) > 1.0


def test_tail_share_is_nan_when_the_total_is_not_positive():
    assert np.isnan(ot.tail_share([-1.0, -2.0, -3.0]))


def test_tail_share_is_one_for_a_single_positive_contributor():
    assert ot.tail_share([0.0, 0.0, 0.0, 5.0], 0.25) == pytest.approx(1.0)


def test_trimmed_mean_drops_both_ends():
    # 10% off each end of 10 sorted values removes one per side.
    sample = [-100.0] + [1.0] * 8 + [100.0]
    assert ot.trimmed_mean(sample, 0.10) == pytest.approx(1.0)


def test_winsorized_mean_clamps_rather_than_drops():
    sample = [1.0] * 19 + [1000.0]
    w = ot.winsorized_mean(sample, 0.05)
    assert w < np.mean(sample)             # outlier's leverage capped
    assert w >= 1.0                        # but still counted, not discarded


def test_percentiles_and_empty_sample_are_nan_safe():
    p = ot.percentiles([1.0, 2.0, 3.0, 4.0, 5.0], qs=(50,))
    assert p[50] == pytest.approx(3.0)
    assert all(np.isnan(v) for v in ot.percentiles([], qs=(5, 50, 95)).values())


def test_tail_helpers_ignore_non_finite_values():
    sample = [1.0, float("nan"), 2.0, float("inf"), 3.0]
    assert ot.trimmed_mean(sample, 0.0) == pytest.approx(2.0)
    assert ot.percentiles(sample, qs=(50,))[50] == pytest.approx(2.0)


# ── cluster bootstrap / power gate ──────────────────────────────────────────

def test_cluster_bootstrap_ci_is_deterministic_under_a_seed():
    vals = list(np.linspace(-1.0, 1.0, 40))
    keys = [f"TEST.{i % 8}" for i in range(40)]
    a = ot.cluster_bootstrap_ci(vals, keys, n=200, seed=7)
    b = ot.cluster_bootstrap_ci(vals, keys, n=200, seed=7)
    assert a == b


def test_cluster_bootstrap_ci_brackets_the_estimate():
    vals = [1.0] * 20 + [3.0] * 20
    keys = [f"TEST.{i % 10}" for i in range(40)]
    est, lo, hi = ot.cluster_bootstrap_ci(vals, keys, n=500, seed=1)
    assert est == pytest.approx(2.0)
    assert lo <= est <= hi


def test_cluster_bootstrap_ci_is_wider_than_iid_when_clusters_are_correlated():
    # Every observation inside a cluster is identical, so the cluster carries no
    # more information than one point. An IID bootstrap would not see that.
    rng = np.random.default_rng(0)
    per_cluster = rng.normal(size=12)
    vals, keys = [], []
    for i, v in enumerate(per_cluster):
        vals.extend([float(v)] * 10)       # 10 duplicates per cluster
        keys.extend([f"TEST.{i}"] * 10)
    _, c_lo, c_hi = ot.cluster_bootstrap_ci(vals, keys, n=2000, seed=3)
    _, i_lo, i_hi = ot.cluster_bootstrap_ci(vals, list(range(len(vals))),
                                            n=2000, seed=3)
    assert (c_hi - c_lo) > (i_hi - i_lo)


def test_cluster_bootstrap_ci_needs_two_clusters():
    est, lo, hi = ot.cluster_bootstrap_ci([1.0, 2.0], ["TEST.1", "TEST.1"], n=10)
    assert all(np.isnan(v) for v in (est, lo, hi))


def test_min_detectable_effect_shrinks_with_more_clusters():
    vals = list(np.linspace(-1.0, 1.0, 100))
    assert ot.min_detectable_effect(vals, n_eff=25) > ot.min_detectable_effect(vals, n_eff=100)


def test_verdict_blocks_when_the_interval_straddles_zero():
    call, blockers = ot.verdict(-0.02, 0.03, n_clusters=100, tail=0.1)
    assert call == "NO CONCLUSION"
    assert any("straddles zero" in b for b in blockers)


def test_verdict_blocks_on_too_few_clusters():
    call, blockers = ot.verdict(0.01, 0.03, n_clusters=5, tail=0.1)
    assert call == "NO CONCLUSION"
    assert any("clusters" in b for b in blockers)


def test_verdict_blocks_when_the_tail_carries_the_result():
    call, blockers = ot.verdict(0.01, 0.03, n_clusters=100, tail=0.80)
    assert call == "NO CONCLUSION"
    assert any("tail" in b for b in blockers)


def test_verdict_supported_only_when_every_gate_passes():
    call, blockers = ot.verdict(0.01, 0.03, n_clusters=100, tail=0.20)
    assert call == "SUPPORTED"
    assert blockers == []


# ── anchor_indices ──────────────────────────────────────────────────────────

def test_anchor_indices_on_a_trading_day():
    # freq="B" from Thu 2026-01-01 → Jan 1, 2, 5, 6, ...  Mon Jan 5 is index 2.
    dates = pd.date_range("2026-01-01", periods=10, freq="B")
    t_sig, i_entry = ot.anchor_indices(dates, pd.Timestamp("2026-01-05"))
    assert (t_sig, i_entry) == (2, 3)


def test_anchor_indices_non_trading_day_resolves_backwards():
    # Sun 2026-01-04 has no bar. t_sig must be the PRIOR bar (Fri Jan 2), not the
    # next one — the old searchsorted(side="left") returned Monday's bar here and
    # Monday's bar for Monday too, conflating the signal bar with the entry bar.
    dates = pd.date_range("2026-01-01", periods=10, freq="B")
    t_sig, i_entry = ot.anchor_indices(dates, pd.Timestamp("2026-01-04"))
    assert (t_sig, i_entry) == (1, 2)
    assert dates[t_sig] == pd.Timestamp("2026-01-02")


def test_anchor_indices_before_series_start_is_negative():
    dates = pd.date_range("2026-01-01", periods=10, freq="B")
    t_sig, _ = ot.anchor_indices(dates, pd.Timestamp("2025-12-01"))
    assert t_sig == -1


def test_anchor_entry_is_the_bar_after_the_signal_bar():
    dates = pd.date_range("2026-01-01", periods=10, freq="B")
    for d in ("2026-01-05", "2026-01-04", "2026-01-08"):
        t_sig, i_entry = ot.anchor_indices(dates, pd.Timestamp(d))
        assert i_entry == t_sig + 1


# ── benchmark routing ───────────────────────────────────────────────────────

def test_bench_map_routes_tsx_and_nyse():
    assert ot._BENCH_BY_EXCHANGE["TSX"] == "XIU.TO"
    assert ot._BENCH_BY_EXCHANGE["NYSE"] == "SPY"


def test_benchmark_choice_changes_the_adjusted_return():
    # A .TO name adjusted against a RISING TSX proxy must not read the same as
    # against a flat SPY — routing has to actually reach the arithmetic.
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.full(n, 100.0)
    close[2 + 21] = 110.0                            # +10% ticker move
    flat = _series(np.full(n, 400.0), dates)
    rising = np.full(n, 400.0)
    rising[2 + 21] = 416.0                           # +4% benchmark over the span
    out_flat = ot.forward_returns(close, dates, flat, 2, horizons=(21,))
    out_rise = ot.forward_returns(close, dates, _series(rising, dates), 2, horizons=(21,))
    assert out_flat["fwd21"] == pytest.approx(0.10)
    assert out_rise["fwd21"] == pytest.approx(0.10 - 0.04)


def test_build_observations_records_the_benchmark_label():
    obs, _ = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "g")], lambda t: _frame(),
        _bench_for(label="XIU.TO"))
    assert obs[0]["bench"] == "XIU.TO"


def test_build_observations_drops_rows_with_no_benchmark():
    obs, stats = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "g")], lambda t: _frame(),
        lambda t: (None, "MISSING"))
    assert obs == []
    assert stats["bad"] == 1


# ── build_observations (dedupe + maturity accounting) ───────────────────────

def _frame(n=60, start="2026-01-01", price=100.0):
    dates = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame({"close": np.full(n, price)}, index=dates)


def _flat_bench(n=60, start="2026-01-01"):
    dates = pd.date_range(start, periods=n, freq="B")
    return pd.Series(np.full(n, 400.0), index=dates)


def _bench_for(series=None, label="SPY"):
    """build_observations' benchmark resolver: ticker -> (series, label)."""
    s = _flat_bench() if series is None else series
    return lambda t: (s, label)


def _row(ticker, date, gate):
    return {"ticker": ticker, "signal_date": date, "gate": gate}


def test_build_observations_dedupes_to_earliest_per_gate_month():
    rows = [
        _row("TEST.1", "2026-01-05", "g"),
        _row("TEST.1", "2026-01-12", "g"),      # same ticker/gate/month → dropped
        _row("TEST.1", "2026-01-19", "g"),      # same again → dropped
    ]
    obs, stats = ot.build_observations(rows, lambda t: _frame(), _bench_for())
    assert len(obs) == 1
    assert obs[0]["signal_date"] == pd.Timestamp("2026-01-05").date()
    assert stats["deduped"] == 1


def test_build_observations_keeps_distinct_gates_and_months():
    rows = [
        _row("TEST.1", "2026-01-05", "g"),
        _row("TEST.1", "2026-01-06", "h"),      # different gate → kept
        _row("TEST.1", "2026-02-03", "g"),      # different month → kept
        _row("TEST.2", "2026-01-05", "g"),      # different ticker → kept
    ]
    obs, stats = ot.build_observations(rows, lambda t: _frame(), _bench_for())
    assert stats["deduped"] == 4
    assert len(obs) == 4


def test_build_observations_counters_are_per_observation_not_per_row():
    # The key is claimed on first ATTEMPT, so 5 rows that collapse to one
    # observation contribute exactly one not-matured count — the old code
    # retried each row and reported an inflated "not matured" headline.
    rows = [_row("TEST.1", f"2026-01-{d:02d}", "g") for d in (5, 6, 7, 8, 9)]
    short = _frame(n=10)                        # no +21d window exists
    obs, stats = ot.build_observations(rows, lambda t: short, _bench_for())
    assert obs == []
    assert stats["deduped"] == 1
    assert stats["not_matured"] == 1


def test_build_observations_accounting_balances():
    # deduped must equal matured + not_matured + missing_price + bad, or the
    # headline is a display lie.
    rows = [
        _row("TEST.1", "2026-01-05", "g"),      # matures
        _row("TEST.2", "2026-01-05", "g"),      # no price cache
        _row("TEST.3", "2026-01-05", "g"),      # too recent
    ]
    frames = {"TEST.1": _frame(), "TEST.2": None, "TEST.3": _frame(n=10)}
    obs, stats = ot.build_observations(rows, frames.get, _bench_for())
    assert stats["deduped"] == 3
    assert (len(obs) + stats["not_matured"]
            + len(stats["missing_price"]) + stats["bad"]) == stats["deduped"]


def test_build_observations_missing_price_recorded_not_counted_as_matured():
    rows = [_row("TEST.9", "2026-01-05", "g")]
    obs, stats = ot.build_observations(rows, lambda t: None, _bench_for())
    assert obs == []
    assert stats["missing_price"] == {"TEST.9"}


def test_build_observations_bad_signal_date_is_sampled():
    rows = [_row("TEST.1", "not-a-date", "g")]
    obs, stats = ot.build_observations(rows, lambda t: _frame(), _bench_for())
    assert obs == []
    assert stats["bad"] == 1
    assert stats["bad_samples"] and "TEST.1" in stats["bad_samples"][0]


def test_build_observations_scores_and_classifies():
    dates = pd.date_range("2026-01-01", periods=60, freq="B")
    close = np.full(60, 100.0)
    i0 = 2                                       # 2026-01-05 is the 3rd business day
    close[i0 + 21] = 120.0                       # +20% over flat SPY
    df = pd.DataFrame({"close": close}, index=dates)
    obs, _ = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "g")], lambda t: df, _bench_for())
    assert len(obs) == 1
    assert obs[0]["fwd21"] == pytest.approx(0.20)
    assert obs[0]["cls"] == "missed_winner"


class _FakeCF:
    """Stand-in for backtest.counterfactual.CounterfactualResult — the replay is
    injected, so the tracker's wiring is testable without the exit ladder."""

    def __init__(self, r=1.5, mfe=2.0, mae=-0.4, reason="target",
                 bars=7, matured=True):
        self.r_multiple, self.mfe_r, self.mae_r = r, mfe, mae
        self.exit_reason, self.bars_held, self.matured = reason, bars, matured


def test_build_observations_attaches_replay_results():
    obs, _ = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "g")], lambda t: _frame(), _bench_for(),
        replay=lambda df, t_sig, tk: _FakeCF())
    assert obs[0]["r_multiple"] == pytest.approx(1.5)
    assert obs[0]["mfe_r"] == pytest.approx(2.0)
    assert obs[0]["exit_reason"] == "target"


def test_build_observations_replay_receives_the_signal_bar():
    seen = {}

    def _spy(df, t_sig, tk):
        seen["t_sig"] = t_sig
        return _FakeCF()

    ot.build_observations([_row("TEST.1", "2026-01-05", "g")],
                          lambda t: _frame(), _bench_for(), replay=_spy)
    # Mon 2026-01-05 is index 2 of the business-day frame — the SIGNAL bar, not
    # the entry bar. The replay owns the T -> T+1 step itself.
    assert seen["t_sig"] == 2


def test_build_observations_unmatured_replay_is_not_an_outcome():
    # An open_eod force-close at the last bar is truncation, not a result, so it
    # must not enter the R statistics.
    obs, _ = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "g")], lambda t: _frame(), _bench_for(),
        replay=lambda df, t_sig, tk: _FakeCF(matured=False, reason="open_eod"))
    assert np.isnan(obs[0]["r_multiple"])
    assert obs[0]["exit_reason"] == "unmatured"


def test_build_observations_replay_failure_is_counted_not_raised():
    def _boom(df, t_sig, tk):
        raise ValueError("bad frame")

    obs, stats = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "g")], lambda t: _frame(), _bench_for(),
        replay=_boom)
    assert stats["bad"] == 1
    assert np.isnan(obs[0]["r_multiple"])       # row survives, R is absent


def test_aggregate_reports_r_space_and_exit_mix():
    obs = [
        {"gate": "g", "fwd21": 0.01, "r_multiple": 2.5, "mfe_r": 2.6,
         "mae_r": -0.2, "exit_reason": "target", "cls": "neutral"},
        {"gate": "g", "fwd21": -0.01, "r_multiple": -1.0, "mfe_r": 0.3,
         "mae_r": -1.0, "exit_reason": "stop", "cls": "neutral"},
        {"gate": "g", "fwd21": 0.0, "r_multiple": float("nan"), "mfe_r": float("nan"),
         "mae_r": float("nan"), "exit_reason": "unmatured", "cls": "neutral"},
    ]
    g = ot.aggregate(obs)["g"]
    assert g["n"] == 3                          # raw-% stats keep all three
    assert g["n_r"] == 2                        # R stats drop the unmatured row
    assert g["median_r"] == pytest.approx(0.75)
    assert g["median_mfe_r"] == pytest.approx(1.45)
    assert dict(g["exit_mix"]) == {"target": 1, "stop": 1}


def test_aggregate_r_space_absent_when_no_replay_ran():
    obs = [{"gate": "g", "fwd21": 0.01, "cls": "neutral"}]
    g = ot.aggregate(obs)["g"]
    assert g["n_r"] == 0
    assert np.isnan(g["median_r"])
    assert g["n"] == 1                          # raw-% side still reports


def test_build_observations_short_gate_flips_classification():
    dates = pd.date_range("2026-01-01", periods=60, freq="B")
    close = np.full(60, 100.0)
    close[2 + 21] = 80.0                         # -20% → a short that worked
    df = pd.DataFrame({"close": close}, index=dates)
    obs, _ = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "hard-to-borrow (short blocked)")],
        lambda t: df, _bench_for())
    assert obs[0]["cls"] == "missed_winner"
