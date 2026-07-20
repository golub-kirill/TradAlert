#!/usr/bin/env python3
"""
Opportunity-cost shadow tracker — what did the scanner's gates cost (or save)?

A read-only postmortem over the live journal. For every name the scanner
*passed on* (recorded in `scan_results` / `scan_runs`) it computes the realized
**market-adjusted forward return** and turns "I skipped a winner" into an honest
two-sided number per rejecting gate: avoided losers vs missed winners.

A row is "passed on" when:
  • `passed = 0`                                      → scan-blocked (gate = its `reason`), OR
  • `passed = 1 AND signal_kind = 'none'`             → passed scan but no entry fired
                                                        (gate = its `reason`), OR
  • `declined = 1`                                    → a FIRED entry the owner skipped via the
                                                        Telegram 🚫 Skip button (gate = 'declined').

Rows that are **not** passed-on and are excluded: exit-signal evaluations
(`signal_kind LIKE 'exit%'`) and hold rows (`no exit condition met`). Both
describe a position already held, where a forward return is not an opportunity
cost. See `is_passed_on`.

Gate normalization: `scan_results.reason` embeds live numbers ("ATR% 0.29 < min
1.0"), so the raw column carries ~1.7k distinct values over ~22k rows — one
bucket per observation, which makes a per-gate rollup meaningless.
`normalize_gate` collapses each reason to a stable gate family, with a
numeric-strip fallback so new reason strings never re-explode the cardinality.

Unattributed rows: when the signal stage left no reason, the journal stores the
scan-*pass* snapshot ("UPTREND | vol×1.2 | RSI 55 | MACD↑"). No gate rejected
those names, so they bucket as `(no gate recorded)` instead of being reported as
a gate that cost you.

Bar anchor: a run's signal bar is the last exchange session COMPLETED as of its
wall-clock `created_at`, via `core.freshness.last_completed_session` — NOT
`DATE(created_at)`. A scan that runs before the close is reading the previous
session's bar. Measured on this journal, the naive date put the signal bar one
bar early on ~47% of rows and correct on ~43% — a coin flip; the session-aware
anchor lands on the right bar 82% of the time. It also handles the NYSE/TSX
holiday divergence, which no fixed offset can.

Benchmark: per listing exchange (`_BENCH_BY_EXCHANGE`) — SPY for NYSE names,
XIU.TO for `.TO` names. 40% of the journalled universe is Canadian, and
adjusting a CAD-priced name against SPY in USD measures the currency and the
wrong market.

For each (ticker, signal_date, gate) the forward return from the signal bar is
market-adjusted vs that ticker's benchmark over the identical span (same `.asof`
approach as `core.pead.car_event`), then classified per gate:
  • > +win  → missed_winner   (the gate cost you)
  • < -lose → avoided_loser   (the gate saved you)
  • else    → neutral

Overlap control: the same name is often blocked on many consecutive days, so the
forward windows overlap heavily. Per-gate stats dedupe to one observation per
(ticker, gate, year-month) — the earliest signal_date that month. The ALL rollup
dedupes further, to one per (ticker, year-month), so a name blocked by two gates
in the same month is not counted twice against a single price move.

    python scripts/live/opportunity_tracker.py
    python scripts/live/opportunity_tracker.py --days-back 90 --win 0.05 --lose 0.05
    python scripts/live/opportunity_tracker.py --min-n 5 --csv docs/backtest_out/opp.csv

Requires DB_* in config/secrets.env (same as the live scanner) and the price cache.
Read-only: never touches the engine/backtester/signal code and never writes the DB.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load DB_* (and other secrets) into the environment so persistence.db_conn sees
# them — mirrors main.py / run_backtest.py, which load this explicitly at startup.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass


# Forward-return horizon every headline stat is quoted on. An observation needs
# this many bars after its reference bar to count as matured.
HORIZON = 21

# Bucket for rows whose journal `reason` is the scan-PASS snapshot: the signal
# stage recorded no reason, so no gate is attributable to the pass-on.
UNATTRIBUTED = "(no gate recorded)"

# Owner pressed 🚫 Skip on a fired entry — not a gate rejection.
DECLINED = "declined (owner skip)"

# Market benchmark per listing exchange (``core.freshness.exchange_for``). 40% of
# the journalled universe is ``.TO``; adjusting a CAD-priced TSX name against
# SPY in USD measures the currency and the wrong market. XIU = TSX 60, which
# matches a liquidity-gated scanner universe — XIC (broad composite) is the
# alternative and is a one-line change here.
_BENCH_BY_EXCHANGE = {"TSX": "XIU.TO", "NYSE": "SPY"}


# ── pure helpers (import-safe; unit-tested; no DB / no network) ──────────────

def forward_returns(close: np.ndarray, dates, bench_close, i0: int,
                    horizons=(5, 21)) -> dict:
    """Market-adjusted forward returns from index ``i0`` (the signal bar).

    For each horizon ``h``::

        mkt_adj_h = (close[i0+h]/close[i0] - 1)
                    - (bench.asof(dates[i0+h])/bench.asof(dates[i0]) - 1)

    ``bench_close`` is the benchmark resolved for this ticker's listing exchange
    (see ``_BENCH_BY_EXCHANGE``), not SPY unconditionally.

    NaN when ``i0+h`` is past the end of the series or any of the four prices is
    non-finite or <= 0 (mirrors ``core.pead.car_event``'s finiteness guards and
    ``.asof`` usage). A signal date predating the benchmark series makes
    ``.asof`` NaN, which correctly NaNs every horizon rather than reporting an
    unadjusted raw return as if it were market-adjusted.

    Also returns ``mdd21`` — the worst close-to-close drawdown over the 21-bar
    window from ``i0`` (the avoided downside), as a non-positive fraction; NaN
    when the full 21-bar window does not exist or contains a bad price.

    Returns a dict ``{"fwd5": ..., "fwd21": ..., "mdd21": ...}`` keyed by
    ``f"fwd{h}"`` for each horizon plus ``mdd21``.
    """
    n = len(close)
    out: dict[str, float] = {}

    in_range = 0 <= i0 < n
    c0 = float(close[i0]) if in_range else float("nan")
    b0 = float(bench_close.asof(dates[i0])) if in_range else float("nan")
    base_ok = (in_range and np.isfinite(c0) and c0 > 0
               and np.isfinite(b0) and b0 > 0)

    for h in horizons:
        j = i0 + h
        if not base_ok or j >= n:
            out[f"fwd{h}"] = float("nan")
            continue
        c_h = float(close[j])
        b_h = float(bench_close.asof(dates[j]))
        if not (np.isfinite(c_h) and c_h > 0 and np.isfinite(b_h) and b_h > 0):
            out[f"fwd{h}"] = float("nan")
            continue
        out[f"fwd{h}"] = (c_h / c0 - 1.0) - (b_h / b0 - 1.0)

    # Worst close-to-close drawdown over the 21-bar window (i0 .. i0+21).
    out["mdd21"] = _max_drawdown(close, i0, HORIZON)
    return out


def _max_drawdown(close: np.ndarray, i0: int, window: int) -> float:
    """Worst close-to-close drawdown (a non-positive fraction) over
    ``close[i0 .. i0+window]``. NaN if the full window is missing or any price in
    it is non-finite/<= 0."""
    n = len(close)
    j_end = i0 + window
    if i0 < 0 or j_end >= n:
        return float("nan")
    seg = np.asarray(close[i0:j_end + 1], dtype=float)
    if not np.all(np.isfinite(seg)) or np.any(seg <= 0):
        return float("nan")
    # Running peak → running drawdown; the minimum is the worst. Non-positive by
    # construction, since the first bar is its own peak.
    peaks = np.maximum.accumulate(seg)
    return min(0.0, float(np.min(seg / peaks - 1.0)))


# Ordered reason → gate-family rules; first match wins, so the specific
# ("overextended short") precedes the general ("overextended"). Patterns are
# anchored on the literal text core/filter_engine.py emits; the numeric-strip
# fallback in normalize_gate() catches anything not listed.
_GATE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # scan-stage quality gates
    (re.compile(r"^price\b.*<\s*min", re.I), "price < min"),
    (re.compile(r"^avg dollar vol\b", re.I), "dollar volume < min"),
    (re.compile(r"^market cap\b", re.I), "market cap < min"),
    (re.compile(r"^ATR%.*<\s*min", re.I), "ATR% < min"),
    (re.compile(r"^ATR%.*>\s*max", re.I), "ATR% > max"),
    # signal-stage gates
    (re.compile(r"^no entry conditions met", re.I), "no entry conditions met"),
    (re.compile(r"^regime\b.*:", re.I), "regime blocks entries"),
    (re.compile(r"^earnings\s+\d+d\s+ago", re.I), "earnings buffer (post)"),
    (re.compile(r"^earnings in\b", re.I), "earnings buffer (pre)"),
    (re.compile(r"^prev bar range\b", re.I), "gap risk: prev bar range"),
    (re.compile(r"^trigger bar red\b", re.I), "anti-gap: trigger bar red"),
    (re.compile(r"^overextended short\b", re.I), "overextension veto (short)"),
    (re.compile(r"^overextended\b", re.I), "overextension veto"),
    (re.compile(r"hard-to-borrow", re.I), "hard-to-borrow (short blocked)"),
    (re.compile(r"^R:R below minimum", re.I), "R:R below minimum"),
    (re.compile(r"sector", re.I), "sector relative strength"),
    # data / pipeline failures — a pass-on, but not a deliberate gate
    (re.compile(r"^only\s+\d+\s+rows|^insufficient data", re.I), "data: insufficient rows"),
    (re.compile(r"freshness|completed sessions", re.I), "data: stale/no fresh bar"),
    (re.compile(r"warmup", re.I), "data: indicators in warmup"),
    (re.compile(r"^cache load failed|^indicator error|^scan exception", re.I),
     "data: pipeline error"),
)

# The scan-PASS snapshot format (filter_engine._scan_pass_reason), e.g.
# "UPTREND | vol×1.24 | RSI 55.1 | MACD↑". Its presence means the signal stage
# left no reason, so there is no gate to attribute.
_SCAN_PASS_SNAPSHOT = re.compile(r"\bvol[×x]\s*[\d.]+", re.I)

# Hold rows: an exit evaluation on a position already held, not a pass-on.
_HOLD_REASON = re.compile(r"no exit condition met", re.I)

# Collapse literal numbers so an unrecognised reason still buckets by template
# instead of one bucket per observation.
_NUMS = re.compile(r"-?\d+(?:[.,]\d+)*%?")


def is_passed_on(*, passed, signal_kind, declined, reason) -> bool:
    """True when a `scan_results` row is a name the scanner passed on.

    Excludes exit-signal evaluations and hold rows — both describe a position
    already held, where a forward return is not an opportunity cost. An
    owner-declined row is always passed-on, whatever its signal_kind.
    """
    if declined:
        return True
    kind = (signal_kind or "none").strip().lower()
    if kind.startswith("exit"):
        return False
    if reason and _HOLD_REASON.search(reason):
        return False
    if not passed:
        return True
    return kind == "none"


def normalize_gate(reason: str | None, *, declined: bool = False) -> str:
    """Collapse a raw `scan_results.reason` to a stable gate family.

    The journal embeds live numbers in the reason ("ATR% 0.29 < min 1.0"), so the
    raw column cannot be grouped. Returns:

      • ``DECLINED``     when the owner skipped a fired entry,
      • ``UNATTRIBUTED`` when the reason is the scan-PASS snapshot or is empty,
      • a named family from ``_GATE_RULES`` when one matches,
      • otherwise the reason with every number replaced by ``#`` (template fallback).
    """
    if declined:
        return DECLINED
    text = (reason or "").strip()
    if not text:
        return UNATTRIBUTED
    if _SCAN_PASS_SNAPSHOT.search(text):
        return UNATTRIBUTED
    for pattern, label in _GATE_RULES:
        if pattern.search(text):
            return label
    return _NUMS.sub("#", text)


# Gate families that can only block a SHORT candidate. A blocked short that fell
# is a missed winner, so its forward return is sign-flipped before classifying.
_SHORT_GATES = frozenset({"overextension veto (short)", "hard-to-borrow (short blocked)"})


def gate_side(gate: str) -> str:
    """``"short"`` for gate families that can only block a short candidate, else
    ``"long"``. Orients the two-sided classifier so "winner" always means "the
    trade would have worked"."""
    return "short" if gate in _SHORT_GATES else "long"


def classify(mkt_adj_fwd21: float, *, win: float = 0.05, lose: float = 0.05,
             side: str = "long") -> str:
    """Two-sided label for a market-adjusted forward return.

    ``"missed_winner"`` if ``> +win``, ``"avoided_loser"`` if ``< -lose``, else
    ``"neutral"``. ``win``/``lose`` are market-adjusted return thresholds
    (default ±5%). ``side="short"`` negates the return first, so a blocked short
    that fell counts as a missed winner. NaN classifies as ``"neutral"``.
    """
    if not np.isfinite(mkt_adj_fwd21):
        return "neutral"
    r = -mkt_adj_fwd21 if side == "short" else mkt_adj_fwd21
    if r > win:
        return "missed_winner"
    if r < -lose:
        return "avoided_loser"
    return "neutral"


# ── tail statistics and power (pure; numpy only) ────────────────────────────
#
# The mean is the wrong headline for this distribution. On the live sample the
# top 5% of observations supply more than the whole total — the body is
# net-negative — so the mean flips sign on a handful of names and reports a
# verdict the sample cannot support. These give the shape instead, and the
# verdict gate below refuses to speak when the evidence is not there.

def _finite_array(values) -> np.ndarray:
    a = np.asarray(list(values), dtype=float)
    return a[np.isfinite(a)]


def trimmed_mean(values, trim: float = 0.10) -> float:
    """Mean after dropping ``trim`` of the sample from EACH end. NaN when the
    trim would empty it."""
    a = np.sort(_finite_array(values))
    n = a.size
    if n == 0:
        return float("nan")
    k = int(n * trim)
    core = a[k:n - k] if n - 2 * k > 0 else a
    return float(np.mean(core)) if core.size else float("nan")


def winsorized_mean(values, limit: float = 0.05) -> float:
    """Mean with the extreme ``limit`` of each tail clamped to the surviving
    percentile rather than dropped — keeps the observation, caps its leverage."""
    a = _finite_array(values)
    if a.size == 0:
        return float("nan")
    lo, hi = np.percentile(a, [100 * limit, 100 * (1 - limit)])
    return float(np.mean(np.clip(a, lo, hi)))


def tail_share(values, frac: float = 0.05) -> float:
    """Share of the total contributed by the top ``frac`` of observations.

    NaN when the total is <= 0 (the ratio has no meaning there). A value above
    1.0 is the diagnostic that matters: it means the rest of the sample is
    net-negative and the headline is carried entirely by the tail.
    """
    a = np.sort(_finite_array(values))
    total = float(np.sum(a))
    if a.size == 0 or total <= 0:
        return float("nan")
    k = max(1, int(a.size * frac))
    return float(np.sum(a[-k:]) / total)


def mean_ex_tail(values, frac: float = 0.05) -> float:
    """Mean after removing the top ``frac`` of observations — the flip
    diagnostic. When this has the opposite sign to the plain mean, the headline
    is a tail artifact and not a property of the population."""
    a = np.sort(_finite_array(values))
    if a.size == 0:
        return float("nan")
    k = max(1, int(a.size * frac))
    core = a[:-k]
    return float(np.mean(core)) if core.size else float("nan")


def percentiles(values, qs=(5, 25, 50, 75, 95)) -> dict:
    """``{q: value}`` for each requested percentile; all NaN on an empty sample."""
    a = _finite_array(values)
    if a.size == 0:
        return {q: float("nan") for q in qs}
    return {q: float(np.percentile(a, q)) for q in qs}


def cluster_bootstrap_ci(values, cluster_keys, stat=None, n: int = 10_000,
                         ci: float = 0.95, seed: int = 42) -> tuple:
    """``(estimate, lo, hi)`` resampling whole CLUSTERS with replacement.

    Observations here are clustered, not a time-ordered series: several names
    share a (ticker, month) window and every name in a month shares residual
    market and sector beta the benchmark adjustment does not remove. An IID
    bootstrap would treat those as independent and report a CI far too narrow.
    Resampling whole ``cluster_keys`` groups is the matching estimator.

    (``backtest.multiple_testing._stationary_bootstrap_indices`` is the
    alternative, but Politis-Romano assumes serial structure in a time-ordered
    series — the wrong shape for a single-year cluster sample.)

    Returns all-NaN with fewer than two non-empty clusters.
    """
    stat = stat or (lambda a: float(np.mean(a)))
    groups: dict = defaultdict(list)
    for v, k in zip(values, cluster_keys):
        if np.isfinite(v):
            groups[k].append(float(v))
    arrs = [np.asarray(g, dtype=float) for g in groups.values() if g]
    if len(arrs) < 2:
        return (float("nan"), float("nan"), float("nan"))

    est = stat(np.concatenate(arrs))
    rng = np.random.default_rng(seed)
    m = len(arrs)
    draws = np.empty(n, dtype=float)
    for i in range(n):
        idx = rng.integers(0, m, m)
        draws[i] = stat(np.concatenate([arrs[j] for j in idx]))
    half = (1.0 - ci) / 2.0
    return (float(est),
            float(np.percentile(draws, 100 * half)),
            float(np.percentile(draws, 100 * (1 - half))))


def min_detectable_effect(values, n_eff: int | None = None,
                          ci: float = 0.95) -> float:
    """Smallest mean effect this sample could distinguish from zero.

    ``z * sd / sqrt(n_eff)``. Pass ``n_eff`` as the CLUSTER count, not the
    observation count — overlapping windows mean the nominal n overstates the
    information available. NaN below two observations.
    """
    from backtest.multiple_testing import norm_ppf
    a = _finite_array(values)
    if a.size < 2:
        return float("nan")
    n = int(n_eff) if n_eff else a.size
    if n < 1:
        return float("nan")
    z = norm_ppf(1.0 - (1.0 - ci) / 2.0)
    return float(z * np.std(a, ddof=1) / np.sqrt(n))


def verdict(lo: float, hi: float, *, n_clusters: int, tail: float,
            min_clusters: int = 20, max_tail_share: float = 0.5) -> tuple:
    """``(verdict, blockers)`` for the headline read.

    Returns ``"NO CONCLUSION"`` whenever the confidence interval straddles zero,
    the cluster count is below ``min_clusters``, or the top-5% tail carries more
    than ``max_tail_share`` of the total. Any one of those makes a directional
    claim unsupportable, and the previous unconditional "gates net-COST you"
    printed off a tail-driven mean is exactly the failure this prevents.
    """
    blockers = []
    if not (np.isfinite(lo) and np.isfinite(hi)):
        blockers.append("no usable confidence interval")
    elif lo <= 0.0 <= hi:
        blockers.append("the confidence interval straddles zero")
    if n_clusters < min_clusters:
        blockers.append(f"{n_clusters} clusters < {min_clusters} required")
    if np.isfinite(tail) and tail > max_tail_share:
        blockers.append(f"top-5% tail carries {tail:.0%} of the total")
    return ("NO CONCLUSION" if blockers else "SUPPORTED"), blockers


def anchor_indices(dates, signal_date) -> tuple[int, int]:
    """``(t_sig, i_entry)`` for a signal session date.

    ``t_sig`` is the last bar on/before ``signal_date`` — the bar whose close sets
    the entry geometry (``core.filter_engine`` builds stop/target off bar T's
    close). ``i_entry`` is the next bar, where the fill lands (the backtester
    enters at T+1's open).

    ``t_sig`` is ``-1`` when no bar exists on/before the date.

    Using ``side="right") - 1`` keeps one meaning for one expression. The former
    ``searchsorted(D, side="left")`` returned the signal bar on a trading day but
    the *entry* bar on a weekend/holiday — two different anchors from one call.
    """
    import pandas as pd
    t_sig = int(dates.searchsorted(pd.Timestamp(signal_date), side="right")) - 1
    return t_sig, t_sig + 1


def _dedupe_by_ticker_month(observations: list[dict]) -> list[dict]:
    """One observation per (ticker, year-month), keeping the first seen.

    A name blocked by two gates in one month yields two rows against a single
    price move; the ALL rollup must not count that twice. Returns the input
    unchanged when rows carry no ticker/scan_date (unit tests pass bare stats).
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for o in observations:
        ticker, date = o.get("ticker"), o.get("signal_date")
        if ticker is None or date is None:
            return list(observations)
        key = (ticker, date.year, date.month)
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


def aggregate(observations: list[dict]) -> dict:
    """Per-gate rollup over observations.

    Each obs has ``gate``, ``fwd5``, ``fwd21``, ``mdd21``, ``cls``. Rows with a
    non-finite ``fwd21`` are dropped from *every* stat, percentages included —
    a NaN-inclusive denominator against a NaN-exclusive count understates both
    percentages. ``dropped`` reports how many were excluded.

    Per gate returns::

        {"n", "dropped", "median_fwd21", "mean_fwd21", "median_fwd5",
         "median_mdd21", "pct_missed_winner", "pct_avoided_loser"}

    where ``mean_fwd21`` is the headline read — negative ⇒ the gate avoided
    losers, positive ⇒ it cost you. The ``"__ALL__"`` rollup is computed over
    observations deduped to one per (ticker, year-month) so overlapping windows
    are not double-counted across gates.
    """
    by_gate: dict[str, list[dict]] = defaultdict(list)
    for o in observations:
        by_gate[o["gate"]].append(o)

    def _finite(rows: list[dict], key: str) -> list[float]:
        return [r[key] for r in rows if np.isfinite(r.get(key, float("nan")))]

    def _med(vals: list[float]) -> float:
        return float(np.median(vals)) if vals else float("nan")

    def _roll(rows: list[dict]) -> dict:
        valid = [r for r in rows if np.isfinite(r.get("fwd21", float("nan")))]
        n = len(valid)
        fwd = [r["fwd21"] for r in valid]
        miss = sum(1 for r in valid if r.get("cls") == "missed_winner")
        avoid = sum(1 for r in valid if r.get("cls") == "avoided_loser")
        return {
            "n": n,
            "dropped": len(rows) - n,
            "median_fwd21": _med(fwd),
            "mean_fwd21": float(np.mean(fwd)) if n else float("nan"),
            "median_fwd5": _med(_finite(valid, "fwd5")),
            "median_mdd21": _med(_finite(valid, "mdd21")),
            "pct_missed_winner": (100.0 * miss / n) if n else 0.0,
            "pct_avoided_loser": (100.0 * avoid / n) if n else 0.0,
        }

    result = {gate: _roll(rows) for gate, rows in by_gate.items()}
    result["__ALL__"] = _roll(_dedupe_by_ticker_month(observations))
    return result


# ── DB + price I/O (main; not import-safe) ──────────────────────────────────

def _fetch_passed_on(conn, days_back: int | None) -> list[dict]:
    """Passed-on rows: (ticker, signal_date, gate). See module docstring for the
    definition. Ordered by created_at, ticker so the monthly dedupe keeps the
    earliest run deterministically.

    ``signal_date`` is the last exchange session COMPLETED as of the run's
    wall-clock ``created_at`` — not ``DATE(created_at)``. A scan that runs before
    the close is looking at the previous session's bar, so the naive date
    mis-anchors it by one bar. Measured on this journal: runs at 20:00-23:00 UTC
    (after the 16:00 ET close) anchor to the same-day bar, runs at 00:00-16:00 to
    the prior bar — overall a coin flip. ``last_completed_session`` also handles
    the NYSE/TSX holiday divergence, which a fixed offset would not.
    """
    from core.freshness import exchange_for, last_completed_session

    cur = conn.cursor(dictionary=True)
    where_days = ""
    params: tuple = ()
    if days_back is not None:
        where_days = " AND r.created_at >= (NOW() - INTERVAL %s DAY) "
        params = (int(days_back),)
    # SQL only narrows to the candidate set; the row-level definition stays in
    # is_passed_on() so the two cannot drift apart.
    cur.execute(
        "SELECT sr.ticker, r.created_at AS scan_ts, sr.reason AS reason, "
        "       sr.passed AS passed, sr.signal_kind AS signal_kind, "
        "       sr.declined AS declined "
        "FROM scan_results sr JOIN scan_runs r ON r.id = sr.run_id "
        "WHERE (sr.passed = 0 OR sr.signal_kind IS NULL "
        "       OR sr.signal_kind = 'none' OR sr.declined = 1) "
        + where_days +
        "ORDER BY r.created_at, sr.ticker",
        params,
    )
    rows = cur.fetchall()
    cur.close()

    # ~113 runs x 2 exchanges, so memoising collapses 20k calendar lookups to ~226.
    session: dict[tuple, object] = {}

    out = []
    for row in rows:
        declined = bool(row.get("declined"))
        if not is_passed_on(passed=row.get("passed"),
                            signal_kind=row.get("signal_kind"),
                            declined=declined,
                            reason=row.get("reason")):
            continue
        ticker = row["ticker"]
        key = (row["scan_ts"], exchange_for(ticker))
        if key not in session:
            session[key] = last_completed_session(row["scan_ts"], key[1])
        out.append({
            "ticker": ticker,
            "signal_date": session[key],
            "gate": normalize_gate(row.get("reason"), declined=declined),
        })
    return out


def _safe_price_path(prices_dir: Path, ticker: str) -> Path:
    """Ticker → cached parquet path, rejecting separators and parent refs before
    the symbol becomes a filename (mirrors ``persistence.cache._path``)."""
    t = str(ticker).upper().strip()
    if (not t) or ("/" in t) or ("\\" in t) or (".." in t) or ("\x00" in t):
        raise ValueError(f"invalid ticker for cache path: {ticker!r}")
    return prices_dir / f"{t}.parquet"


def _naive_index(pd, index):
    """DatetimeIndex normalized to tz-naive. The cache is written tz-naive today;
    this keeps searchsorted/.asof from raising if a fetcher ever writes tz-aware."""
    idx = pd.to_datetime(index)
    return idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx


def build_observations(passed_on: list[dict], load_prices, bench_for, *,
                       win: float = 0.05, lose: float = 0.05) -> tuple[list[dict], dict]:
    """Score passed-on rows into matured, deduped observations.

    ``load_prices`` maps a ticker to its price DataFrame (or None when the cache
    has no usable file). ``bench_for`` maps a ticker to its
    ``(benchmark_close_series, benchmark_label)`` — see ``_BENCH_BY_EXCHANGE``;
    a ``.TO`` name is adjusted against the TSX, not against SPY in USD.
    Returns ``(observations, stats)`` where stats carries ``deduped`` /
    ``not_matured`` / ``missing_price`` / ``bad`` / ``bad_samples``.

    The (ticker, gate, year-month) key is claimed on the first ATTEMPT rather
    than on success, so a later date in the same month can never silently
    substitute for the earliest one and every counter is per-observation instead
    of per-row — ``deduped`` equals the sum of the outcome buckets.
    """
    import pandas as pd

    observations: list[dict] = []
    stats = {"deduped": 0, "not_matured": 0, "missing_price": set(),
             "bad": 0, "bad_samples": []}
    seen_keys: set[tuple] = set()

    def _note(msg: str) -> None:
        stats["bad"] += 1
        if len(stats["bad_samples"]) < 3:
            stats["bad_samples"].append(msg)

    for rec in passed_on:
        ticker, gate = rec["ticker"], rec["gate"]
        try:
            d = pd.Timestamp(rec["signal_date"]).normalize()
        except (ValueError, TypeError) as exc:
            _note(f"{ticker}: bad signal_date ({exc})")
            continue

        # Rows arrive created_at-ascending, so the first key seen is the earliest.
        key = (ticker, gate, d.year, d.month)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        df = load_prices(ticker)
        if df is None:
            stats["missing_price"].add(ticker)
            continue
        bench_close, bench_label = bench_for(ticker)
        if bench_close is None:
            _note(f"{ticker}: no benchmark series")
            continue
        try:
            dates = df.index
            close = df["close"].to_numpy(dtype=float)
            # t_sig = signal bar (its close sets the geometry); i_entry = fill bar.
            t_sig, i_entry = anchor_indices(dates, d)
            if t_sig < 0:
                _note(f"{ticker}: no bar on/before {d.date()}")
                continue
            # The window is measured from the fill bar, so maturity keys off
            # i_entry — one bar stricter than anchoring on the signal bar.
            if i_entry + HORIZON >= len(close):
                stats["not_matured"] += 1
                continue
            fr = forward_returns(close, dates, bench_close, t_sig, horizons=(5, HORIZON))
        except (KeyError, ValueError, TypeError) as exc:
            _note(f"{ticker} @ {d.date()}: {exc}")
            continue

        observations.append({
            "ticker": ticker,
            "gate": gate,
            "signal_date": d.date(),
            "bench": bench_label,
            "t_sig": t_sig,
            "i_entry": i_entry,
            "fwd5": fr["fwd5"],
            "fwd21": fr["fwd21"],
            "mdd21": fr["mdd21"],
            "cls": classify(fr["fwd21"], win=win, lose=lose, side=gate_side(gate)),
        })

    stats["deduped"] = len(seen_keys)
    return observations, stats


def _fmt_row(label: str, a: dict) -> str:
    """One fixed-width table row. Non-finite stats render as 'n/a' rather than
    'nan%' so an empty gate cannot be misread as a real number."""
    def pct(v: float, w: int) -> str:
        return f"{v:>+{w}.2%}" if np.isfinite(v) else f"{'n/a':>{w}}"

    mean = a["mean_fwd21"]
    if not np.isfinite(mean):
        read = ""
    elif mean < 0:
        read = "avoided losers"
    elif mean > 0:
        read = "cost you"
    else:
        read = "flat"
    name = label if len(label) <= 32 else label[:29] + "..."
    return (f"{name:<32} {a['n']:>4} {pct(a['median_fwd5'], 9)} "
            f"{pct(a['median_fwd21'], 10)} {pct(mean, 11)} "
            f"{a['pct_missed_winner']:>7.0f}% {a['pct_avoided_loser']:>8.0f}% "
            f"{pct(a['median_mdd21'], 8)}  {read}")


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Opportunity-cost shadow tracker (read-only postmortem)")
    ap.add_argument("--days-back", type=int, default=None,
                    help="Limit history to the last N days (default: all). Needs to "
                         "exceed ~30 calendar days or nothing will have matured.")
    ap.add_argument("--prices-dir", default="data/prices",
                    help="Directory of cached {TICKER}.parquet price files.")
    ap.add_argument("--win", type=float, default=0.05,
                    help="Missed-winner threshold (market-adj fwd21 return). Default 0.05.")
    ap.add_argument("--lose", type=float, default=0.05,
                    help="Avoided-loser threshold (market-adj fwd21 return). Default 0.05.")
    ap.add_argument("--min-n", type=int, default=1,
                    help="Hide gates with fewer than N matured observations. Default 1.")
    ap.add_argument("--csv", default=None,
                    help="Write the per-observation rows to this CSV "
                         "(manual run results belong under docs/backtest_out/).")
    ap.add_argument("--bootstrap", type=int, default=10_000,
                    help="Cluster-bootstrap draws for the confidence interval. "
                         "Default 10000.")
    args = ap.parse_args()

    if args.win < 0 or args.lose < 0:
        print("  ✗ --win and --lose are magnitudes and must be >= 0.")
        return
    if args.days_back is not None and args.days_back <= 0:
        print("  ✗ --days-back must be positive.")
        return

    import pandas as pd
    from persistence.db_conn import connect

    prices_dir = (Path(args.prices_dir) if Path(args.prices_dir).is_absolute()
                  else _ROOT / args.prices_dir)

    try:
        conn = connect()
    except Exception as exc:
        print(f"  ✗ DB connect failed ({exc}). Set DB_* in config/secrets.env.")
        return
    try:
        passed_on = _fetch_passed_on(conn, args.days_back)
    finally:
        conn.close()

    if not passed_on:
        print("  No passed-on history yet — run the daily scan to accumulate "
              "(python main.py).")
        return

    # Market benchmarks — one per listing exchange, each loaded once as a close
    # Series for .asof lookups. Hard-fail on a missing one: silently falling back
    # to SPY for a .TO name is the CAD-vs-USD bug this routing exists to remove.
    from core.freshness import exchange_for

    bench_close: dict[str, "pd.Series"] = {}
    for sym in sorted(set(_BENCH_BY_EXCHANGE.values())):
        b_path = prices_dir / f"{sym}.parquet"
        if not b_path.exists():
            print(f"  ✗ benchmark {sym} missing at {b_path} — cannot market-adjust. "
                  f"Populate the price cache first.")
            return
        b_df = pd.read_parquet(b_path)
        b_df.index = _naive_index(pd, b_df.index)
        bench_close[sym] = b_df["close"].sort_index()

    def _bench_for(ticker: str):
        sym = _BENCH_BY_EXCHANGE[exchange_for(ticker)]
        return bench_close.get(sym), sym

    # Per-ticker price cache so each parquet is read once across many scan dates.
    price_cache: dict[str, "pd.DataFrame | None"] = {}

    def _load_prices(ticker: str):
        if ticker not in price_cache:
            price_cache[ticker] = None
            try:
                p = _safe_price_path(prices_dir, ticker)
            except ValueError as exc:
                print(f"  · {exc}")
                return None
            if p.exists():
                try:
                    d = pd.read_parquet(p)
                    d.index = _naive_index(pd, d.index)
                    price_cache[ticker] = d.sort_index()
                except Exception as exc:
                    print(f"  · price cache unreadable for {ticker}: {exc}")
        return price_cache[ticker]

    raw_count = len(passed_on)
    observations, stats = build_observations(
        passed_on, _load_prices, _bench_for, win=args.win, lose=args.lose)
    deduped = stats["deduped"]
    not_matured = stats["not_matured"]
    missing_price = stats["missing_price"]
    bad, bad_samples = stats["bad"], stats["bad_samples"]

    if not observations:
        print(f"\n  Passed-on rows: {raw_count}  ·  deduped {deduped}  ·  matured (21d): 0")
        details = []
        if not_matured:
            details.append(f"{not_matured} too recent (no +21d window yet)")
        if missing_price:
            details.append(f"{len(missing_price)} ticker(s) missing price cache")
        if bad:
            details.append(f"{bad} skipped (bad row/price)")
        if details:
            print("  " + "   ".join(details))
        for s in bad_samples:
            print(f"    · {s}")
        print("\n  ⚠ Nothing matured yet — keep the scanner running and rerun once "
              "passed-on names age ~21 trading days.\n")
        return

    agg = aggregate(observations)

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.is_absolute():
            csv_path = _ROOT / csv_path
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(observations).to_csv(csv_path, index=False)
        print(f"  → {len(observations)} observation(s) written to {csv_path}")

    dates_seen = [o["signal_date"] for o in observations]
    print(f"\n  Opportunity-cost shadow tracker  ·  passed-on rows {raw_count}  ·  "
          f"deduped {deduped}  ·  matured {len(observations)}  ·  "
          f"{min(dates_seen)} → {max(dates_seen)}  ·  win>+{args.win:.0%} lose<-{args.lose:.0%}")
    bench_split = Counter(o["bench"] for o in observations)
    print("  benchmarks: " + " · ".join(
        f"{sym} ({n} obs)" for sym, n in sorted(bench_split.items())))
    extra = []
    if not_matured:
        extra.append(f"{not_matured} not matured")
    if missing_price:
        # Delisted names have no parquet and drop out here. They are
        # disproportionately the losers, so this count is a survivorship read,
        # not a footnote.
        extra.append(f"{len(missing_price)} missing price (delisted/uncached)")
    if bad:
        extra.append(f"{bad} bad/skipped")
    if extra:
        print("  (" + ", ".join(extra) + ")")
    for s in bad_samples:
        print(f"    · {s}")

    print("\n" + "=" * 104)
    print(f"  {'Gate':<32} {'n':>4} {'med fwd5':>9} {'med fwd21':>10} "
          f"{'mean fwd21':>11} {'%missed':>8} {'%avoided':>9} {'med mdd':>8}  read")
    print("  " + "-" * 100)

    hidden = 0
    gate_keys = [g for g in agg if g != "__ALL__"]
    gate_keys.sort(key=lambda g: (-agg[g]["n"], g))
    for g in gate_keys:
        if agg[g]["n"] < args.min_n:
            hidden += 1
            continue
        print("  " + _fmt_row(g, agg[g]))

    print("  " + "-" * 100)
    allr = agg["__ALL__"]
    print("  " + _fmt_row("ALL (dedup ticker-month)", allr))
    print("=" * 104)

    if hidden:
        print(f"\n  ({hidden} gate(s) hidden below --min-n {args.min_n}.)")

    # ── power: what this sample can and cannot support ──────────────────────
    # Cluster by TICKER. The ALL rollup has already deduped to one row per
    # (ticker, month), so clustering on that key would give singleton clusters
    # and silently degrade to an IID bootstrap. Ticker is the real repeated
    # measurement. Same-month cross-sectional dependence (residual sector beta
    # the benchmark does not remove) is NOT modelled, so the interval below is a
    # lower bound on the true width — clustering on month instead would be
    # stricter but leaves ~2 clusters on a two-month journal.
    all_rows = _dedupe_by_ticker_month(observations)
    vals = [o["fwd21"] for o in all_rows]
    clusters = [o["ticker"] for o in all_rows]
    n_clusters = len({c for c, v in zip(clusters, vals) if np.isfinite(v)})
    est, lo, hi = cluster_bootstrap_ci(vals, clusters, n=args.bootstrap)
    tail = tail_share(vals, 0.05)
    ex_tail = mean_ex_tail(vals, 0.05)
    mde = min_detectable_effect(vals, n_eff=n_clusters)
    unattr = sum(1 for o in observations if o["gate"] == UNATTRIBUTED)

    print("\n  POWER")
    print(f"    {len(observations)} matured obs across {n_clusters} ticker "
          f"clusters; {100.0 * unattr / max(1, len(observations)):.0f}% carry no "
          f"attributable gate.")
    if np.isfinite(mde):
        print(f"    smallest detectable mean effect at 95%: ±{mde:.2%}")
    if np.isfinite(est) and np.isfinite(lo):
        print(f"    ALL mean {est:+.2%}  95% CI [{lo:+.2%}, {hi:+.2%}]  "
              f"(cluster bootstrap, {args.bootstrap} draws)")
    if np.isfinite(tail):
        print(f"    top 5% carries {tail:.0%} of the total; mean excluding it "
              f"{ex_tail:+.2%}")

    call, blockers = verdict(lo, hi, n_clusters=n_clusters, tail=tail)
    if blockers:
        print(f"\n  => {call}: " + "; ".join(blockers) + ".")
        print("     This sample cannot tell a positive edge from a negative one. "
              "No directional\n     claim about any gate is supportable yet.")
    else:
        mean = allr["mean_fwd21"]
        direction = ("net-AVOIDED losers — the passed-on book underperformed "
                     "its benchmark") if mean < 0 else (
                    "net-COST you — the passed-on book beat its benchmark")
        print(f"\n  => {call}: gates {direction} ({mean:+.2%} mean mkt-adj fwd21).")

    print(f"\n  (negative mean ⇒ the gate avoided losers; positive ⇒ it cost you a winner.)\n"
          f"  '{UNATTRIBUTED}' = the journal kept no signal-stage reason for those rows,\n"
          f"  so no gate is attributable — they are not evidence about any gate.\n")


if __name__ == "__main__":
    main()
