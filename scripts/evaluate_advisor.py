#!/usr/bin/env python3
"""
Prospective AI-advisor evaluation — does the verdict predict realized R?

The honest measurement of advisor value (docs/AI_ADVISOR_PLAN.md): every
``advisor_note`` in ``scan_results`` was journaled at decision time with news
fetched at decision time, so verdict-vs-outcome here is a clean, time-aligned
prediction — unlike any backtest replay, which is structurally look-ahead
(scripts/test_advisor.py is a plumbing smoke test only).

Scoring reuses ``reconcile_live``'s replay verbatim (T+1 open entry, configured
slippage/commission, stored stop/target geometry, shared ``core.exits``
time-stop) so the advisor meter and the drift meter agree by construction.
A signal is *resolved* once it hits stop/target/cap; *pending* otherwise.

Reported per verdict bucket (agree/flag/disagree) vs the contemporaneous
base rate (ALL fired entries in the same window, advised or not):
  • n, total R, E[R], median R, win rate, owner-declined share
  • separation:  E[R|agree] − E[R|disagree]  with a seeded bootstrap CI
  • counterfactual filters: total R taking everything vs skipping disagree
    (and disagree+flag) — the advisor's implied filter value
  • confidence calibration bands (<70 / 70–85 / ≥85%) for agree + disagree

Honesty guards (pre-registered, from the plan): resolved n < 50 → the verdict
is DIRECTIONAL ONLY; n < 30 → INSUFFICIENT, no conclusions. Known limitation:
whether headlines were present for a given prompt is not journaled, so the
news-vs-technical-only split is not measurable from the current schema.

    python scripts/evaluate_advisor.py
    python scripts/evaluate_advisor.py --since 2026-07-01 --include-review

Requires DB_* in config/secrets.env and the price cache. Read-only on the DB.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src"), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass

# Pre-registered sample gates (AI_ADVISOR_PLAN: "after 50+ live signals").
N_TARGET = 50   # below this: directional only
N_FLOOR = 30    # below this: insufficient, print no separation verdict

_VERDICTS = ("agree", "flag", "disagree")
# "✅ Agree · 82% — reasoning  ⚠ risks" (service.format_note). Word-boundary +
# capitalized labels; Disagree listed first so its "agree" tail can't shadow it.
_NOTE_RE = re.compile(r"\b(Disagree|Agree|Flag)\b\s*·\s*(\d{1,3})%")


# ── pure helpers (unit-tested in tests/test_evaluate_advisor.py) ─────────────

def parse_note(note: str | None) -> tuple[str | None, float | None]:
    """Extract (verdict, confidence) from a journaled advisor_note.

    Returns (None, None) for empty/unparseable notes — those rows fall into
    the '(no verdict)' bucket rather than being dropped.
    """
    if not note:
        return None, None
    m = _NOTE_RE.search(note)
    if not m:
        return None, None
    return m.group(1).lower(), min(1.0, max(0.0, int(m.group(2)) / 100.0))


def bucket_stats(rows: list[dict]) -> dict[str, dict]:
    """Aggregate resolved rows ({verdict, r, declined}) per verdict bucket.

    Returns {bucket: {n, tot, mean, med, wr, declined}} including a '(none)'
    bucket for unparseable/missing verdicts and an 'ALL' base-rate bucket.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["verdict"] or "(none)"].append(row)
    groups["ALL"] = list(rows)

    out: dict[str, dict] = {}
    for name, grp in groups.items():
        rs = sorted(row["r"] for row in grp)
        n = len(rs)
        if not n:
            continue
        out[name] = {
            "n": n,
            "tot": sum(rs),
            "mean": sum(rs) / n,
            "med": rs[n // 2] if n % 2 else (rs[n // 2 - 1] + rs[n // 2]) / 2,
            "wr": sum(1 for r in rs if r > 0) / n,
            "declined": sum(1 for row in grp if row.get("declined")),
        }
    return out


def counterfactuals(rows: list[dict]) -> list[tuple[str, float, int]]:
    """Total R of simple verdict-filter policies over the resolved sample."""
    policies = [
        ("take everything", lambda v: True),
        ("skip disagree", lambda v: v != "disagree"),
        ("skip disagree+flag", lambda v: v not in ("disagree", "flag")),
    ]
    out = []
    for name, keep in policies:
        kept = [row["r"] for row in rows if keep(row["verdict"])]
        out.append((name, sum(kept), len(kept)))
    return out


def confidence_bands(rows: list[dict], verdict: str) -> list[tuple[str, int, float, float]]:
    """(band, n, win-rate, E[R]) for one verdict's confidence bands."""
    bands = [("<70%", 0.0, 0.70), ("70-85%", 0.70, 0.85), (">=85%", 0.85, 1.01)]
    out = []
    for label, lo, hi in bands:
        rs = [row["r"] for row in rows
              if row["verdict"] == verdict and row["conf"] is not None
              and lo <= row["conf"] < hi]
        if rs:
            out.append((label, len(rs),
                        sum(1 for r in rs if r > 0) / len(rs),
                        sum(rs) / len(rs)))
    return out


def bootstrap_diff_ci(a: list[float], b: list[float], iters: int = 10_000,
                      seed: int = 1337, alpha: float = 0.10) -> tuple[float, float] | None:
    """Percentile-bootstrap CI for mean(a) − mean(b). None when either side < 5."""
    if len(a) < 5 or len(b) < 5:
        return None
    import random

    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        ra = [rng.choice(a) for _ in a]
        rb = [rng.choice(b) for _ in b]
        diffs.append(sum(ra) / len(ra) - sum(rb) / len(rb))
    diffs.sort()
    lo_i = int(len(diffs) * (alpha / 2))
    hi_i = int(len(diffs) * (1 - alpha / 2)) - 1
    return diffs[lo_i], diffs[hi_i]


# ── DB fetch + replay scoring ────────────────────────────────────────────────

def _fetch_entries(conn, since: str | None, include_review: bool) -> list[dict]:
    """All fired entries with their advisor_note (may be NULL). Raises on a
    pre-migration DB (no advisor_note column) — main() turns that into advice."""
    tier_filter = "" if include_review else " AND (sr.tier IS NULL OR sr.tier = 'LIVE') "
    since_filter = " AND r.created_at >= %(since)s " if since else ""
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT sr.ticker, sr.signal_kind, sr.close, sr.atr, "
            "       sr.stop_price, sr.target_price, sr.signal_type, "
            "       sr.declined, sr.advisor_note, r.created_at "
            "FROM scan_results sr JOIN scan_runs r ON r.id = sr.run_id "
            "WHERE sr.passed = 1 AND sr.signal_kind IN ('entry_long','entry_short')"
            + tier_filter + since_filter +
            "ORDER BY r.created_at, sr.ticker",
            {"since": since} if since else {},
        )
        return cur.fetchall()
    finally:
        cur.close()


def _score(sigs: list[dict], cfg: dict, max_hold: int, mode: str):
    """Replay each fired entry to realized R via reconcile_live's machinery.

    Returns (resolved_rows, pending, errors). Each resolved row carries
    {verdict, conf, r, declined, ticker, date, reason}.
    """
    import pandas as pd

    from persistence.cache import load as cache_load
    from backtest.backtester import (adjust_target_for_slippage, apply_stop_fill,
                                     apply_stop_fill_short, apply_target_fill,
                                     apply_target_fill_short)
    from reconcile_live import _replay  # single source of replay-scoring truth

    resolved: list[dict] = []
    pending = errors = 0
    for s in sigs:
        try:
            df = cache_load(s["ticker"])
        except Exception:
            errors += 1
            continue
        D = pd.Timestamp(s["created_at"]).normalize()
        entry_idx = int(df.index.searchsorted(D, side="right"))  # T+1 open
        if entry_idx >= len(df):
            pending += 1
            continue
        entry = float(df.iloc[entry_idx]["open"])
        is_short = s["signal_kind"] == "entry_short"

        # Advisor-era rows always journal geometry; guard anyway (fail-open).
        if s["stop_price"] is None or s["target_price"] is None:
            errors += 1
            continue
        stop, target = float(s["stop_price"]), float(s["target_price"])

        slip = cfg["entry_slippage_pct"]
        if slip:
            entry *= (1.0 - slip) if is_short else (1.0 + slip)
            target = adjust_target_for_slippage(
                entry, stop, target, cfg["min_rr"],
                direction="short" if is_short else "long")

        risk = (stop - entry) if is_short else (entry - stop)
        if risk <= 0:
            errors += 1
            continue

        exit_price, reason = _replay(df, entry_idx, entry, stop, target, is_short,
                                     max_hold, mode,
                                     apply_stop_fill, apply_target_fill,
                                     apply_stop_fill_short, apply_target_fill_short)
        if exit_price is None:
            pending += 1
            continue
        r = ((entry - exit_price) / risk) if is_short else ((exit_price - entry) / risk)
        r -= cfg["commission_r"]

        verdict, conf = parse_note(s["advisor_note"])
        resolved.append({
            "verdict": verdict, "conf": conf, "r": r,
            "declined": bool(s.get("declined")),
            "ticker": s["ticker"], "date": D.date(), "reason": reason,
        })
    return resolved, pending, errors


# ── report ───────────────────────────────────────────────────────────────────

_EMOJI = {"agree": "✅", "flag": "⚠️", "disagree": "❌", "(none)": "  ", "ALL": "  "}


def _print_report(resolved: list[dict], pending: int, errors: int,
                  max_hold: int, mode: str) -> None:
    n = len(resolved)
    # The pre-registered gates count resolved VERDICTS, never the '(none)'
    # base-rate rows — otherwise an advisor outage (NULL notes) inflates n and
    # silently defeats the honesty guard.
    n_verdicts = sum(1 for row in resolved if row["verdict"] is not None)
    dates = [row["date"] for row in resolved]
    print(f"\n  Advisor evaluation  ·  {n} resolved fired entries "
          f"({n_verdicts} with a verdict)  ·  "
          f"{min(dates)} → {max(dates)}  ·  cap {max_hold}d {mode}")
    print(f"  Pending (too recent): {pending}   Errors/skipped: {errors}")

    if n_verdicts < N_FLOOR:
        print(f"\n  ⚠ INSUFFICIENT SAMPLE — {n_verdicts} < {N_FLOOR} resolved verdicts. "
              f"No conclusions; keep the live feed running.")
    elif n_verdicts < N_TARGET:
        print(f"\n  ⚠ SMALL SAMPLE — {n_verdicts} < {N_TARGET} resolved verdicts "
              f"(pre-registered target). Directional only, not evidence.")

    stats = bucket_stats(resolved)
    print(f"\n  {'Verdict':<14} {'n':>4} {'tot R':>8} {'E[R]':>8} {'med R':>8} "
          f"{'WR':>5}  {'declined':>8}")
    print("  " + "-" * 62)
    order = [v for v in (*_VERDICTS, "(none)") if v in stats] + ["ALL"]
    for name in order:
        st = stats[name]
        label = f"{_EMOJI.get(name, '')} {name}"
        print(f"  {label:<14} {st['n']:>4} {st['tot']:>+8.2f} {st['mean']:>+8.3f} "
              f"{st['med']:>+8.3f} {st['wr']:>4.0%}  {st['declined']:>8}")
    print("  " + "-" * 62)

    # Separation — the headline question: do verdicts order outcomes? A
    # directional conclusion additionally needs the verdict floor, ≥10 per
    # bucket, and a non-degenerate CI; below N_TARGET the wording stays soft.
    agree_rs = [row["r"] for row in resolved if row["verdict"] == "agree"]
    dis_rs = [row["r"] for row in resolved if row["verdict"] == "disagree"]
    if agree_rs and dis_rs:
        sep = sum(agree_rs) / len(agree_rs) - sum(dis_rs) / len(dis_rs)
        ci = bootstrap_diff_ci(agree_rs, dis_rs)
        ci_str = f"   (bootstrap 90% CI [{ci[0]:+.3f}, {ci[1]:+.3f}])" if ci else \
            "   (CI n/a — a bucket has < 5 resolved)"
        print(f"\n  Separation  E[R|agree] − E[R|disagree] = {sep:+.3f}R{ci_str}")
        conclusive = (n_verdicts >= N_FLOOR and ci is not None and ci[0] != ci[1]
                      and len(agree_rs) >= 10 and len(dis_rs) >= 10)
        if conclusive:
            firm = n_verdicts >= N_TARGET
            if ci[0] > 0:
                print("  → verdicts ORDER outcomes correctly at this sample (CI > 0)."
                      if firm else
                      "  → directionally consistent (CI > 0) — below the "
                      f"{N_TARGET}-verdict target, treat as tentative.")
            elif ci[1] < 0:
                print("  → verdicts order outcomes BACKWARDS at this sample (CI < 0)."
                      if firm else
                      "  → directionally BACKWARDS (CI < 0) — below the "
                      f"{N_TARGET}-verdict target, treat as tentative.")
            else:
                print("  → no reliable separation at this sample (CI spans 0).")
    else:
        print("\n  Separation n/a — need at least one resolved agree AND disagree.")

    print("\n  Counterfactual filters (total R over the resolved sample):")
    base = None
    for name, tot, kept in counterfactuals(resolved):
        if base is None:
            base = tot
            print(f"    {name:<22} {tot:>+8.2f}R  (n={kept})")
        else:
            print(f"    {name:<22} {tot:>+8.2f}R  (n={kept})  Δ {tot - base:+.2f}R")

    for verdict in ("agree", "disagree"):
        bands = confidence_bands(resolved, verdict)
        if bands:
            cells = " · ".join(f"{b}: n={bn} WR {wr:.0%} E[R] {er:+.2f}"
                               for b, bn, wr, er in bands)
            print(f"\n  Confidence calibration ({verdict}):  {cells}")

    print("\n  Limitation: headline presence per prompt is not journaled — the "
          "news-vs-technicals split is not measurable from this schema.\n")


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Prospective advisor evaluation (live journal)")
    ap.add_argument("--since", default=None, help="only runs on/after this date (YYYY-MM-DD)")
    ap.add_argument("--include-review", action="store_true",
                    help="include NEEDS_REVIEW fires (stale/gapped data; excluded by default)")
    ap.add_argument("--max-hold-days", type=int, default=None,
                    help="time-stop cap override (default: execution.max_hold_days)")
    args = ap.parse_args()

    from persistence.db_conn import connect
    from reconcile_live import _cfg  # same config read as the drift meter

    cfg = _cfg()
    max_hold = args.max_hold_days if args.max_hold_days is not None else cfg["max_hold_days"]
    mode = cfg["max_hold_mode"]

    try:
        conn = connect()
    except Exception as exc:
        print(f"  ✗ DB connect failed ({exc}). Set DB_* in config/secrets.env.")
        return
    try:
        entries = _fetch_entries(conn, args.since, args.include_review)
    except Exception as exc:
        if getattr(exc, "errno", None) == 1054:
            # The SELECT names several optional columns — say WHICH one is missing
            # instead of always blaming advisor_note.
            m = re.search(r"Unknown column '(?:sr\.)?(\w+)'", str(exc))
            col = m.group(1) if m else "?"
            if col == "advisor_note":
                print("  ✗ scan_results has no advisor_note column — apply the ALTER "
                      "in data/scan_schema.sql, enable the advisor, and let live "
                      "scans accrue.")
            else:
                print(f"  ✗ scan_results is missing the '{col}' column — this DB "
                      f"predates a recorded migration (see data/scan_schema.sql).")
        else:
            print(f"  ✗ query failed: {exc}")
        return
    finally:
        conn.close()

    advised = [e for e in entries if e["advisor_note"]]
    if not advised:
        print("  No journaled advisor verdicts yet — enable advisor.enabled in "
              "settings.yaml and let daily scans accrue (plan target: 50+).")
        return

    # Base rate = ALL fired entries contemporaneous with the advised sample, so
    # advisor-error rows ('(none)' bucket) stay in and the comparison is honest.
    window_start = min(e["created_at"] for e in advised)
    sample = [e for e in entries if e["created_at"] >= window_start]

    resolved, pending, errors = _score(sample, cfg, max_hold, mode)
    if not resolved:
        print(f"  {len(advised)} advised entries journaled ({len(sample)} fired in "
              f"window), none resolved yet (pending {pending}, errors {errors}) — "
              f"signals need ~{max_hold} trading days to mature. Keep scanning.")
        return

    _print_report(resolved, pending, errors, max_hold, mode)


if __name__ == "__main__":
    main()
