"""Pure helpers of scripts/live/evaluate_advisor.py (no DB / no network / no LLM).

Covers the note parser (the journal-string contract with service.format_note),
bucket aggregation, counterfactual filters, confidence bands, and the seeded
bootstrap CI — the math the prospective advisor verdict will rest on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "live"))

from evaluate_advisor import (  # noqa: E402
    bootstrap_diff_ci,
    bucket_stats,
    confidence_bands,
    counterfactuals,
    parse_note,
)


def _row(verdict, r, conf=0.8, declined=False):
    return {"verdict": verdict, "conf": conf, "r": r, "declined": declined}


# ── parse_note: the contract with service.format_note ────────────────────────

def test_parse_agree_note():
    assert parse_note("✅ Agree · 82% — strong momentum") == ("agree", 0.82)


def test_parse_disagree_note_not_shadowed_by_agree():
    # "Disagree" contains "agree" — the parser must not mislabel it.
    assert parse_note("❌ Disagree · 95% — invalidated by news") == ("disagree", 0.95)


def test_parse_flag_note_with_risks_tail():
    v, c = parse_note("⚠️ Flag · 65% — earnings soon  ⚠ gap risk")
    assert v == "flag" and c == 0.65


def test_parse_note_matches_real_format_note_output():
    # Lock the parser to the ACTUAL live formatter, not a hand-typed copy.
    from core.advisor.schemas import AdvisorVerdict
    from core.advisor.service import format_note

    note = format_note(AdvisorVerdict("disagree", 0.9, "weak setup", risks="event"))
    assert parse_note(note) == ("disagree", 0.9)


def test_parse_empty_and_unparseable():
    assert parse_note("") == (None, None)
    assert parse_note(None) == (None, None)
    assert parse_note("some legacy free text") == (None, None)


def test_parse_confidence_clamped():
    assert parse_note("✅ Agree · 130% — over-eager")[1] == 1.0


def test_parse_rubric_note_with_scorecard():
    # Current hybrid-rubric format: N/10 conviction + axis scorecard + risks.
    note = ("✅ Agree · 8/10 — momentum intact  "
            "[edge✓ trend✓ ext· liq✗ R:R✓ event·]  ⚠ earnings soon")
    assert parse_note(note) == ("agree", 0.8)


def test_parse_rubric_disagree_and_full_conviction():
    assert parse_note("❌ Disagree · 3/10 — thesis broken") == ("disagree", 0.3)
    assert parse_note("⚠️ Flag · 10/10 — event window") == ("flag", 1.0)


# ── bucket_stats ─────────────────────────────────────────────────────────────

def test_bucket_stats_groups_and_base_rate():
    rows = [_row("agree", 2.0), _row("agree", -1.0),
            _row("disagree", -1.0), _row(None, 0.5, conf=None)]
    st = bucket_stats(rows)
    assert st["agree"]["n"] == 2 and st["agree"]["tot"] == 1.0
    assert st["agree"]["wr"] == 0.5
    assert st["disagree"]["mean"] == -1.0
    assert st["(none)"]["n"] == 1          # unparseable notes stay visible
    assert st["ALL"]["n"] == 4             # base rate spans every bucket


def test_bucket_stats_median_even_and_odd():
    st = bucket_stats([_row("agree", r) for r in (1.0, 2.0, 4.0)])
    assert st["agree"]["med"] == 2.0
    st = bucket_stats([_row("agree", r) for r in (1.0, 3.0)])
    assert st["agree"]["med"] == 2.0


def test_bucket_stats_counts_declined():
    st = bucket_stats([_row("agree", 1.0, declined=True), _row("agree", 1.0)])
    assert st["agree"]["declined"] == 1


# ── counterfactuals ──────────────────────────────────────────────────────────

def test_counterfactual_filters():
    rows = [_row("agree", 2.0), _row("flag", 0.5),
            _row("disagree", -1.0), _row(None, 1.0)]
    cf = dict((name, (tot, n)) for name, tot, n in counterfactuals(rows))
    assert cf["take everything"] == (2.5, 4)
    assert cf["skip disagree"] == (3.5, 3)          # dropped the -1.0R
    assert cf["skip disagree+flag"] == (3.0, 2)     # also dropped the +0.5R flag


def test_counterfactual_none_bucket_never_skipped():
    # A '(none)' verdict is an advisor failure, not advice — no policy drops it.
    rows = [_row(None, -2.0)]
    for _name, tot, n in counterfactuals(rows):
        assert (tot, n) == (-2.0, 1)


# ── confidence bands ─────────────────────────────────────────────────────────

def test_confidence_bands_split():
    rows = [_row("agree", 1.0, conf=0.60), _row("agree", -1.0, conf=0.80),
            _row("agree", 2.0, conf=0.90), _row("agree", 1.0, conf=1.00),
            _row("disagree", -1.0, conf=0.90)]      # other verdicts excluded
    bands = {b: (n, wr, er) for b, n, wr, er in confidence_bands(rows, "agree")}
    assert bands["<70%"][0] == 1
    assert bands["70-85%"] == (1, 0.0, -1.0)
    assert bands[">=85%"][0] == 2 and bands[">=85%"][2] == 1.5


def test_confidence_bands_skip_missing_conf():
    assert confidence_bands([_row("agree", 1.0, conf=None)], "agree") == []


# ── bootstrap CI ─────────────────────────────────────────────────────────────

def test_bootstrap_ci_none_under_min_n():
    assert bootstrap_diff_ci([1.0] * 4, [0.0] * 10) is None


def test_bootstrap_ci_separated_samples():
    a = [1.0, 1.2, 0.8, 1.1, 0.9, 1.0, 1.3, 0.7]
    b = [-1.0, -0.8, -1.2, -0.9, -1.1, -1.0, -0.7, -1.3]
    lo, hi = bootstrap_diff_ci(a, b, iters=2000)
    assert lo > 0 and hi > lo                        # clear separation → CI > 0


def test_bootstrap_ci_deterministic_for_seed():
    a = [0.5, -0.2, 1.0, 0.1, 0.4, -0.6, 0.9, 0.2]
    b = [0.3, -0.1, 0.2, -0.4, 0.6, 0.0, -0.3, 0.1]
    assert bootstrap_diff_ci(a, b, iters=1000) == bootstrap_diff_ci(a, b, iters=1000)


def test_bootstrap_ci_overlapping_samples_spans_zero():
    a = [0.1, -0.1, 0.2, -0.2, 0.0, 0.1, -0.1, 0.0]
    b = [0.0, 0.1, -0.1, 0.0, 0.2, -0.2, 0.1, -0.1]
    lo, hi = bootstrap_diff_ci(a, b, iters=2000)
    assert lo < 0 < hi


# ── _score: replay scoring on synthetic prices (no DB / no network) ──────────
# Deterministic OHLC where the outcome arithmetic is exact. Fill model per
# backtest.backtester: intraday touch fills at the level, gap-through at open.

import datetime as _dt  # noqa: E402

import pandas as pd  # noqa: E402

from evaluate_advisor import _score  # noqa: E402

_SCAN_DAY = _dt.datetime(2026, 5, 1, 22, 0)  # post-close scan on Fri May 1
_D1, _D2, _D3, _D4 = (pd.Timestamp("2026-05-04"), pd.Timestamp("2026-05-05"),
                      pd.Timestamp("2026-05-06"), pd.Timestamp("2026-05-07"))


def _cfg0(**over):
    cfg = {"entry_slippage_pct": 0.0, "commission_r": 0.0, "min_rr": 2.5}
    cfg.update(over)
    return cfg


def _sig(**over):
    s = {"ticker": "TEST.1", "signal_kind": "entry_long", "close": 100.0,
         "atr": 2.0, "stop_price": 95.0, "target_price": 110.0,
         "signal_type": "momentum", "declined": 0,
         "advisor_note": "✅ Agree · 82% — ok", "created_at": _SCAN_DAY}
    s.update(over)
    return s


def _df(bars):
    """bars: list of (open, high, low, close), one per day from _D1."""
    idx = [_D1, _D2, _D3, _D4][:len(bars)]
    return pd.DataFrame(
        [{"open": o, "high": h, "low": lo, "close": c} for o, h, lo, c in bars],
        index=idx)


def _patch_prices(monkeypatch, df):
    import persistence.cache
    monkeypatch.setattr(persistence.cache, "load", lambda ticker: df)


def test_score_long_target_hit_exact_r(monkeypatch):
    # T+1 open 100 (no touch), next bar tags 110 intraday → r = 10/5 = 2.0.
    _patch_prices(monkeypatch, _df([(100, 100.5, 99.5, 100.2),
                                    (101, 111, 100, 110)]))
    resolved, pending, errors = _score([_sig()], _cfg0(), 25, "if_not_profit")
    assert (pending, errors) == (0, 0)
    row = resolved[0]
    assert row["r"] == 2.0 and row["reason"] == "target"
    assert row["verdict"] == "agree" and row["conf"] == 0.82


def test_score_long_stop_hit_minus_one_r(monkeypatch):
    _patch_prices(monkeypatch, _df([(100, 100.5, 99.5, 100.2),
                                    (99, 100, 94, 96)]))
    resolved, *_ = _score([_sig()], _cfg0(), 25, "if_not_profit")
    assert resolved[0]["r"] == -1.0 and resolved[0]["reason"] == "stop"


def test_score_short_target_hit(monkeypatch):
    # Short: entry 100, stop 105, target 90 → risk 5; tag 90 intraday → r=2.0.
    _patch_prices(monkeypatch, _df([(100, 101, 99, 100),
                                    (98, 99, 89, 91)]))
    sig = _sig(signal_kind="entry_short", stop_price=105.0, target_price=90.0,
               advisor_note="❌ Disagree · 95% — bad")
    resolved, *_ = _score([sig], _cfg0(), 25, "if_not_profit")
    assert resolved[0]["r"] == 2.0 and resolved[0]["verdict"] == "disagree"


def test_score_commission_subtracted(monkeypatch):
    _patch_prices(monkeypatch, _df([(100, 100.5, 99.5, 100.2),
                                    (101, 111, 100, 110)]))
    resolved, *_ = _score([_sig()], _cfg0(commission_r=0.005), 25, "if_not_profit")
    assert abs(resolved[0]["r"] - 1.995) < 1e-9


def test_score_slippage_reanchors_target_to_min_rr(monkeypatch):
    # Slipped entry 100.2; target re-anchored so a clean tag yields exactly min_rr.
    _patch_prices(monkeypatch, _df([(100, 100.5, 99.5, 100.2),
                                    (100.3, 120, 100, 118)]))
    resolved, *_ = _score([_sig()], _cfg0(entry_slippage_pct=0.002), 25, "if_not_profit")
    assert abs(resolved[0]["r"] - 2.5) < 1e-9


def test_score_time_stop_if_not_profit(monkeypatch):
    # Never touches stop/target; at the 2-bar cap the close is a loss → cut.
    _patch_prices(monkeypatch, _df([(100, 101, 98, 99),
                                    (99, 100, 97.5, 98),
                                    (98, 99, 96.5, 97)]))
    resolved, *_ = _score([_sig()], _cfg0(), 2, "if_not_profit")
    assert resolved[0]["reason"] == "time_stop"
    assert abs(resolved[0]["r"] - (-0.6)) < 1e-9  # (97-100)/5


def test_score_if_not_profit_lets_winner_run_but_hard_cuts(monkeypatch):
    # Profitable at the cap: if_not_profit keeps holding (→ pending here),
    # hard cuts at the cap close. A regression hardcoding either mode fails.
    bars = _df([(100, 101, 99, 100.5),
                (101, 102, 100, 101.5),
                (102, 103, 101, 102.5)])   # in profit at the 2-bar cap
    _patch_prices(monkeypatch, bars)
    resolved, pending, _ = _score([_sig()], _cfg0(), 2, "if_not_profit")
    assert (resolved, pending) == ([], 1)          # winner keeps running
    _patch_prices(monkeypatch, bars)
    resolved, pending, _ = _score([_sig()], _cfg0(), 2, "hard")
    assert pending == 0 and resolved[0]["reason"] == "time_stop"
    assert abs(resolved[0]["r"] - 0.5) < 1e-9      # (102.5-100)/5


def test_score_pending_when_immature(monkeypatch):
    _patch_prices(monkeypatch, _df([(100, 101, 99, 100.5)]))
    resolved, pending, errors = _score([_sig()], _cfg0(), 25, "if_not_profit")
    assert (resolved, pending, errors) == ([], 1, 0)


def test_score_pending_when_no_bar_after_scan(monkeypatch):
    df = _df([(100, 101, 99, 100.5)])
    _patch_prices(monkeypatch, df)
    sig = _sig(created_at=_dt.datetime(2026, 5, 10, 22, 0))  # after last bar
    resolved, pending, errors = _score([sig], _cfg0(), 25, "if_not_profit")
    assert (resolved, pending, errors) == ([], 1, 0)


def test_score_missing_geometry_counts_error(monkeypatch):
    _patch_prices(monkeypatch, _df([(100, 101, 99, 100), (101, 111, 100, 110)]))
    resolved, pending, errors = _score([_sig(stop_price=None)], _cfg0(), 25,
                                       "if_not_profit")
    assert (resolved, pending, errors) == ([], 0, 1)


# ── report smoke (capsys) ────────────────────────────────────────────────────

def test_print_report_small_sample_banner(capsys):
    from evaluate_advisor import _print_report

    rows = [dict(_row("agree", 1.0), ticker="TEST.1", date=_dt.date(2026, 6, 1),
                 reason="target") for _ in range(6)]
    _print_report(rows, pending=2, errors=0, max_hold=25, mode="if_not_profit")
    out = capsys.readouterr().out
    assert "INSUFFICIENT" in out and "6 resolved" in out


def test_print_report_full_sample_separation(capsys):
    from evaluate_advisor import _print_report

    # Varied r values: identical values would make every bootstrap resample
    # equal → a zero-width CI, which the degenerate-CI guard treats as n/a.
    rows = ([dict(_row("agree", 1.0 + 0.1 * (i % 3)), ticker="T",
                  date=_dt.date(2026, 6, 1), reason="target") for i in range(30)]
            + [dict(_row("disagree", -1.0 - 0.1 * (i % 3)), ticker="T",
                    date=_dt.date(2026, 6, 2), reason="stop") for i in range(25)])
    _print_report(rows, pending=0, errors=0, max_hold=25, mode="if_not_profit")
    out = capsys.readouterr().out
    assert "Separation" in out and "ORDER outcomes correctly" in out
    assert "skip disagree" in out and "INSUFFICIENT" not in out


def test_print_report_none_rows_do_not_inflate_the_honesty_gate(capsys):
    # Advisor-outage scenario (review finding, reproduced): 43 NULL-note rows +
    # 12 verdicts must still trip INSUFFICIENT — the gate counts VERDICTS.
    from evaluate_advisor import _print_report

    rows = ([dict(_row(None, 0.2, conf=None), ticker="T", date=_dt.date(2026, 6, 1), reason="target")] * 43
            + [dict(_row("agree", 1.0), ticker="T", date=_dt.date(2026, 6, 1), reason="target")] * 7
            + [dict(_row("disagree", -1.0), ticker="T", date=_dt.date(2026, 6, 2), reason="stop")] * 5)
    _print_report(rows, pending=0, errors=0, max_hold=25, mode="if_not_profit")
    out = capsys.readouterr().out
    assert "INSUFFICIENT" in out and "12 < 30" in out
    assert "ORDER outcomes correctly" not in out    # no conclusion on 12 verdicts
    assert "55 resolved fired entries (12 with a verdict)" in out


def test_score_details_collects_pending_and_errors(monkeypatch):
    # --verbose accrual detail: pending/error records carry ticker + why.
    _patch_prices(monkeypatch, _df([(100, 101, 99, 100.5)]))
    details = {}
    _score([_sig(), _sig(ticker="TEST.2", stop_price=None)],
           _cfg0(), 25, "if_not_profit", details=details)
    assert details["pending"][0]["ticker"] == "TEST.1"
    assert "no stop/target/cap" in details["pending"][0]["why"]
    assert details["errors"][0]["ticker"] == "TEST.2"
    assert "geometry" in details["errors"][0]["why"]


def test_print_report_verbose_ledger(capsys):
    from evaluate_advisor import _print_report

    rows = [dict(_row("agree", 2.0), ticker="TEST.1", date=_dt.date(2026, 6, 1),
                 reason="target", kind="entry_long", declined=True),
            dict(_row("disagree", -1.0), ticker="TEST.2", date=_dt.date(2026, 6, 2),
                 reason="stop", kind="entry_long")]
    details = {"pending": [{"ticker": "TEST.3", "date": _dt.date(2026, 6, 30),
                            "why": "open 3 bar(s), no stop/target/cap yet"}]}
    _print_report(rows, pending=1, errors=0, max_hold=25, mode="if_not_profit",
                  verbose=True, details=details)
    out = capsys.readouterr().out
    assert "Ledger (resolved, chronological):" in out
    assert "TEST.1" in out and "+2.00" in out and "target" in out
    assert "✗" in out                       # declined marker
    assert "Pending (1):" in out and "TEST.3" in out


def test_print_report_non_verbose_has_no_ledger(capsys):
    from evaluate_advisor import _print_report

    rows = [dict(_row("agree", 2.0), ticker="TEST.1", date=_dt.date(2026, 6, 1),
                 reason="target", kind="entry_long")]
    _print_report(rows, pending=0, errors=0, max_hold=25, mode="if_not_profit")
    assert "Ledger" not in capsys.readouterr().out


def test_print_report_tentative_wording_between_floor_and_target(capsys):
    # 30-49 verdicts: CI prints, but the conclusion must be soft, not definitive.
    from evaluate_advisor import _print_report

    rows = ([dict(_row("agree", 1.0 + 0.1 * (i % 3)), ticker="T",
                  date=_dt.date(2026, 6, 1), reason="target") for i in range(20)]
            + [dict(_row("disagree", -1.0 - 0.1 * (i % 3)), ticker="T",
                    date=_dt.date(2026, 6, 2), reason="stop") for i in range(15)])
    _print_report(rows, pending=0, errors=0, max_hold=25, mode="if_not_profit")
    out = capsys.readouterr().out
    assert "SMALL SAMPLE" in out
    assert "tentative" in out and "ORDER outcomes correctly" not in out
