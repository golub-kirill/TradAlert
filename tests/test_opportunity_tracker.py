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
        {"ticker": "TEST.1", "gate": "a", "scan_date": day,
         "fwd5": 0.0, "fwd21": 0.20, "mdd21": -0.01, "cls": "missed_winner"},
        {"ticker": "TEST.1", "gate": "b", "scan_date": day,
         "fwd5": 0.0, "fwd21": 0.20, "mdd21": -0.01, "cls": "missed_winner"},
        {"ticker": "TEST.2", "gate": "a", "scan_date": day,
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


# ── build_observations (dedupe + maturity accounting) ───────────────────────

def _frame(n=60, start="2026-01-01", price=100.0):
    dates = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame({"close": np.full(n, price)}, index=dates)


def _flat_spy(n=60, start="2026-01-01"):
    dates = pd.date_range(start, periods=n, freq="B")
    return pd.Series(np.full(n, 400.0), index=dates)


def _row(ticker, date, gate):
    return {"ticker": ticker, "scan_date": date, "gate": gate}


def test_build_observations_dedupes_to_earliest_per_gate_month():
    rows = [
        _row("TEST.1", "2026-01-05", "g"),
        _row("TEST.1", "2026-01-12", "g"),      # same ticker/gate/month → dropped
        _row("TEST.1", "2026-01-19", "g"),      # same again → dropped
    ]
    obs, stats = ot.build_observations(rows, lambda t: _frame(), _flat_spy())
    assert len(obs) == 1
    assert obs[0]["scan_date"] == pd.Timestamp("2026-01-05").date()
    assert stats["deduped"] == 1


def test_build_observations_keeps_distinct_gates_and_months():
    rows = [
        _row("TEST.1", "2026-01-05", "g"),
        _row("TEST.1", "2026-01-06", "h"),      # different gate → kept
        _row("TEST.1", "2026-02-03", "g"),      # different month → kept
        _row("TEST.2", "2026-01-05", "g"),      # different ticker → kept
    ]
    obs, stats = ot.build_observations(rows, lambda t: _frame(), _flat_spy())
    assert stats["deduped"] == 4
    assert len(obs) == 4


def test_build_observations_counters_are_per_observation_not_per_row():
    # The key is claimed on first ATTEMPT, so 5 rows that collapse to one
    # observation contribute exactly one not-matured count — the old code
    # retried each row and reported an inflated "not matured" headline.
    rows = [_row("TEST.1", f"2026-01-{d:02d}", "g") for d in (5, 6, 7, 8, 9)]
    short = _frame(n=10)                        # no +21d window exists
    obs, stats = ot.build_observations(rows, lambda t: short, _flat_spy())
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
    obs, stats = ot.build_observations(rows, frames.get, _flat_spy())
    assert stats["deduped"] == 3
    assert (len(obs) + stats["not_matured"]
            + len(stats["missing_price"]) + stats["bad"]) == stats["deduped"]


def test_build_observations_missing_price_recorded_not_counted_as_matured():
    rows = [_row("TEST.9", "2026-01-05", "g")]
    obs, stats = ot.build_observations(rows, lambda t: None, _flat_spy())
    assert obs == []
    assert stats["missing_price"] == {"TEST.9"}


def test_build_observations_bad_scan_date_is_sampled():
    rows = [_row("TEST.1", "not-a-date", "g")]
    obs, stats = ot.build_observations(rows, lambda t: _frame(), _flat_spy())
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
        [_row("TEST.1", "2026-01-05", "g")], lambda t: df, _flat_spy())
    assert len(obs) == 1
    assert obs[0]["fwd21"] == pytest.approx(0.20)
    assert obs[0]["cls"] == "missed_winner"


def test_build_observations_short_gate_flips_classification():
    dates = pd.date_range("2026-01-01", periods=60, freq="B")
    close = np.full(60, 100.0)
    close[2 + 21] = 80.0                         # -20% → a short that worked
    df = pd.DataFrame({"close": close}, index=dates)
    obs, _ = ot.build_observations(
        [_row("TEST.1", "2026-01-05", "hard-to-borrow (short blocked)")],
        lambda t: df, _flat_spy())
    assert obs[0]["cls"] == "missed_winner"
