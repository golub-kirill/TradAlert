# Verification & Validation Results — 2026-06 fixes

Final pass over the raw-notes fixes (max-hold exit, sweep dead-key, report
coloring, chronic-penalty de-fang, sweep/walk-forward parallelism). Applies four
lenses: **verification** (logs), **code review**, **reality-check / de-biasing**,
and **debug** (issues found & fixed). Companion docs: `ADR-001-max-hold-exit.md`,
`triage_raw_notes_2026-06.md`, `verification_runbook_2026-06.md`.

## TL;DR verdict

- **All fixes verified correct.** `pytest` 197 passed; the max-hold cap is airtight
  (every `time_stop` at exactly the horizon, max hold = cap); the dead-key sweep row
  now moves; logs are clean; the chronic de-fang behaves; `--workers` and the fast
  walk-forward both work.
- **Headline (25-bar, hard) is a real but *modest* edge that passes the
  temporal-stability gate** (≈0 IS→OOS degradation) — but is **not yet fully
  de-biased**: survivorship (Phase A) and parameter-selection (Phase D) remain open.
- **Recommendation: ship the fixes; treat the 25d-hard number as provisional**
  pending the survivorship and re-tune/multiple-testing gates.

---

## 1. Verification — per step

| Step | What | Result |
|------|------|--------|
| 1 | `pytest tests/` | ✅ **197 passed**; 4 max-hold + 2 new ticker-health tests green |
| 2 | OFF baseline | ✅ no `time_stop`, max bars_held **129** (unbounded) → exit truly off |
| 3 | hard 30 | ✅ `time_stop` all at exactly 30; **max hold 30 across every exit** |
| 4 | hard 10→30 curve | ✅ monotonic; PF>1 at every horizon; **25d** best risk-adj |
| 5 | sweep dead-key | ✅ `behavioral.size_mult_floor` now varies E[R]: 0.25/0.5/0.65 → +103.6/+107.3/+109.5R (breadth penalty identical = dormant, as diagnosed) |
| 6 | report coloring | ✅ piped log has **0** ANSI escape bytes; colors only on a TTY |
| 7 | chronic A/B | ✅ de-fanged ON: Sharpe 0.59→0.60, Calmar 0.12→0.13, maxDD 31.2→29.3R, +1R |
| V5a | 25d hard run | ✅ 1209 trades, +85.7R, Sharpe ~0.57, cap airtight at 25 |
| V5b | walk-forward (45 win) | ✅ ran fixed-config OOS; 62% OOS+, degradation **+0.004** |
| — | V5c robustness, re-tune WF | ◻ not yet run |

The cap behaviour is the strongest correctness signal: with the exit OFF, trades
ran to **129** bars; at `--max-hold-days 25/30` the **maximum hold across all exit
types equals the cap**, and every `time_stop` fires at exactly the cap — so nothing
leaks past the horizon and the OFF path is bit-for-bit the old behaviour.

## 2. Code review (session changes)

**Scope reviewed:** `trade.py` (+1), `backtester.py` (+25), `portfolio_backtester.py`
(+52), `sweep.py` (+54), `run_backtest.py` (+57), `report.py` (+29),
`walk_forward.py` (+7), `filters.yaml` (+34), `ticker_health.py` (+15),
`test_ticker_health.py`, `test_max_hold_exit.py` (new), `scripts/*` (new).

### Correctness
- **No look-ahead introduced.** The time-stop uses `searchsorted(entry_date)` and
  the current bar only; fill at that bar's close. Stop/target are checked first
  (pessimistic precedence) — confirmed by the ledger.
- **Opt-in is real.** `max_hold_days=None` ⇒ baseline replays identically (Step 2).
- **Parallelism fix is process-safe.** `_WORKER_UNIVERSE` is a per-process global set
  once via the pool `initializer`; processes don't share memory, so there's no race.
  A `None` guard raises loudly rather than producing wrong numbers.
- **Chronic block capability retained** (configurable `4: 0.0`) even though the
  default no longer blocks; covered by an explicit test.

### Suggestions (non-blocking)
| # | File | Issue | Category |
|---|------|-------|----------|
| 1 | portfolio_backtester.py / backtester.py | The ~12-line `time_stop` block is **triplicated** across `run_all`, `run_prepped`, `_walk` (mirrors the existing stop/target duplication). Extract a shared `_maybe_time_stop(...)` helper. | Maintainability |
| 2 | sweep.py | Per-worker universe is now shipped once (fixed). For very large universes the TODO's `shared_memory` approach would avoid the N×copy entirely. | Performance |
| 3 | run_backtest.py | UTF-8 `reconfigure` is duplicated across entry points; a tiny shared helper would DRY it. | Maintainability |

### What looks good
- Tight, additive, OFF-by-default changes; strong test coverage (197 green);
  clear comments documenting *why* (e.g. the de-fang rationale, the legacy
  `signals.momentum.short` = fade-exit note).

### Verdict: **Approve.** No critical/security issues (local backtester, no
untrusted input). Address suggestion #1 when convenient.

### Housekeeping flag
The working tree mixes our changes with **pre-existing uncommitted work** (behavioral
fetchers, `behavioral/__init__.py`, and deletions of `FIX_PLAN.md`,
`validation_program_design.md`, `tests/test_regression_fixes.py`). Stage
deliberately — don't `git commit -a` blind. Note `validation_program_design.md` is
*deleted* in the tree but still referenced by TODO; decide whether to restore it.

## 3. Reality check & de-biasing

The point of de-biasing: the in-sample headline is optimistic by construction.
Each test below shrinks it toward what's trustworthy.

| Test | Result | Reading |
|------|--------|---------|
| Trade-level t-stat | **3.39** (n=1209, mean +0.091R) | Significant, but assumes IID trades — overstates (trades overlap/cluster) |
| Monthly Sharpe (ann.) | **0.57** | Modest |
| Deflated Sharpe | **~0.44** | After haircut for the 5 horizons tried |
| Walk-forward OOS+ | **62%** (28/45), binomial p≈**0.068** | Marginally beats coin-flip |
| IS→OOS degradation | **+0.004 E[R]** (≈0) | **Strongest signal** — the fixed config does *not* decay out-of-sample |
| Frictions | slippage 0.001 + commission 0.005 **ON** | Real costs already included |

**Interpretation.** The edge is genuinely present but thin. The most reassuring
result is the near-zero IS→OOS degradation across 45 windows: the *shipped* 25d-hard
config holds up on data it wasn't fit to. The weakest: OOS-positive is only
marginally significant (p≈0.068), and the deflated Sharpe (~0.44) is well below the
uncapped headline (0.59).

> **Metrics methodology (2026-06-04, `stats_utils`).** Sharpe and Sortino are now
> computed on the monthly-R series with **risk-free = 0**, annualised by √12, making
> them **scale-invariant** (independent of the deployed risk fraction). Sortino uses
> the textbook **target downside deviation over /N** — squared shortfall below 0
> averaged over *all* months, not only the down-months. The prior code subtracted a 5%
> cash rate converted at a hardcoded "1R ≈ 10% of equity" (inconsistent with the
> 1%-fixed-risk policy) and averaged Sortino downside over the down-month count only.
> **Every Sharpe/Sortino figure above and in `ADR-001` predates this fix**: they tick
> **up** slightly on the next run (rf=0 drops a ~0.04 R/mo hurdle → Sharpe ≈ +0.05; /N
> raises Sortino). The transform is monotonic, so all *relative* comparisons (mode A
> vs B, OFF vs ON, horizon sweep) are unchanged — only refresh the absolute figures
> when a headline run is next journaled.
>
> **Refreshed headline (2026-06-04, rf=0, `run_id=6`).** Re-ran the canonical
> `--max-hold-days 25 --max-hold-mode hard` (full history): **Sharpe 0.58, Sortino
> 1.02**, +87.5R over 1211 trades (WR 48.8%, PF 1.28, Calmar 0.12; `time_stop` 193t @
> 80% WR). This is the rf=0/`/N` value for the shipped config; the old-convention
> tables above are left as-is (internally consistent), so compare to them only on
> *direction*, not absolute level. Other configs (OFF baseline, `if_not_profit`, the
> 10–30d sweep, deflated Sharpe) are not yet re-journaled under rf=0.
>
> **rf=0 refresh + friction bump (2026-06-05).** The "other configs" above are now refreshed
> (OFF baseline, `if_not_profit`, the 10–30d sweep) — see the **rf=0 refresh** table in
> `ADR-001`. They were re-run at the **new** `entry_slippage_pct=0.002` default, so the
> slippage bump (not the metric) drives most of the change. At slippage 0.002 the 25d-**hard**
> config is +43.8R/0.29 (down from `run_id=6`'s +87.5R/0.58 at slippage 0.001). Deflated Sharpe
> still awaits Phase D. Relative rankings (if_not_profit > hard; 25d near-peak; PF > 1) hold.
>
> **New trading default (2026-06-05): 25d `if_not_profit` @ budget 5.0.** Switched from `hard`
> (a validation-conservatism choice) to the economically-correct "let winners run" exit, which
> dominates on every metric: **headline now +74.5R, Sharpe 0.50, PF 1.26** (vs hard +43.8R/0.29).
> Budget re-validated at the new config (`scripts/budget_sweep.py`): Sharpe peaks at 5.0. See
> `ADR-001` *Decision update* for the caveat — the extra edge leans on the unvalidated long-hold
> tail, so forward-test via `reconcile_fills.py` before sizing up.
>
> **Scoring OFF + v3 universe → current headline (2026-06-05, `run_id=11`, ADR-003).** Two more
> changes landed: the watchlist grew to 213 names (deep Canadian bench, no survivorship pruning),
> and the `SignalScorer` was made opt-in and turned OFF by default (its entry score is
> non-predictive of R, corr −0.03, and ranking by it picked weaker trades). On the broad universe
> with scoring off: **+116.7R, Sharpe 0.66, Sortino 1.16, PF 1.30, E[R] +0.075** over 1560 trades,
> bootstrap E[R] CI [+0.036, +0.113]. Scoring-ON control (`run_id=10`) on the same code: +68.9R,
> Sharpe 0.42 — so −40% of the edge was the scoring layer. This is the broad/honest universe, so
> the gain is not survivorship. **This is the current headline.**
>
> **OOS validation — fixed-config walk-forward (2026-06-06, `run_id=12`).** Ran `--walk-forward
> --wf-no-retune --workers 14` on the scoring-OFF headline: 47 rolling 3yr-IS/1yr-OOS windows.
> **IS E[R] +0.066 → OOS +0.072 (degradation −0.006 — OOS slightly *better*); 32/47 = 68% of OOS
> windows profitable, binomial p ≈ 0.009.** Improves on the prior 25d-hard V5b (62%, p ≈ 0.068).
> Conclusion: the headline edge is **temporally stable** — not in-sample luck. **Caveat:** this is
> the fixed-config test only; the parameter-selection / data-snooping correction (full re-tune
> walk-forward = V5, + deflated Sharpe = Phase D) is still pending before the edge is fully
> de-biased.
>
> **Cap change → risk budget (2026-06-04, `run_id=7`).** The portfolio cap was then
> re-expressed from a raw count (`max_concurrent=6`) to an aggregate-risk budget
> (`max_open_risk=6.0`, in `size_mult` units). Re-running the same 25d-hard headline:
> **+95.7R / 1365 trades, Sharpe 0.55, Sortino 0.97** (PF 1.27, Calmar 0.12, WR 48.6%;
> `time_stop` 213t @ 81%). vs `run_id=6`: +154 trades (+13%), +8.2R (+9%), Sharpe
> −0.03. Reading: the budget admits more concurrent positions when they're sized down
> in cautious regimes — more deployment and absolute R, marginally lower risk-adjusted.
>
> **Budget sweep → default lowered to 5.0 (2026-06-04).** Swept `max_open_risk` at the
> 25d-hard config (exploratory, un-journaled):
>
> | Budget | Trades | Total R | PF | Sharpe | Sortino | Calmar |
> |--------|--------|---------|----|--------|---------|--------|
> | 4.0 | 983 | +55.7 | 1.22 | 0.44 | 0.74 | 0.09 |
> | **5.0** | 1194 | +87.2 | **1.29** | **0.58** | **1.03** | 0.12 |
> | 6.0 | 1365 | +95.7 | 1.27 | 0.55 | 0.97 | 0.12 |
> | 8.0 | 1633 | +98.9 | 1.23 | 0.48 | 0.85 | 0.12 |
>
> Sharpe/Sortino/PF all peak at **5.0**; total R keeps rising with budget but quality
> erodes. 5.0 reproduces the old count-cap=6 posture (`run_id=6`: +87.5R/0.58) under
> the principled risk-budget mechanism. Default set to **5.0** (NS#1: risk-adjusted),
> giving up ~9% of total R vs 6.0 for +0.03 Sharpe. *Caveat:* this is the in-sample
> Sharpe-max — 5.0 is defensible as a round number matching the prior effective level,
> not a fine-tuned peak.
>
> **OOS confirmation (2026-06-05, fixed-config walk-forward, 45 windows, 3yr IS /
> 1yr OOS / 6mo step).** The in-sample preference for 5.0 holds out-of-sample —
> 5.0 beats 6.0 on every OOS axis:
>
> | Budget | Avg OOS E[R] | Degradation IS→OOS | OOS profitable |
> |--------|--------------|--------------------|----------------|
> | **5.0** | **+0.054** | **+0.004** | **67%** (30/45) |
> | 6.0 | +0.048 | +0.008 | 62% (28/45) |
>
> Near-zero degradation (+0.004) → 5.0 does not decay OOS, so the choice was *not*
> in-sample snooping; it also generalises better than 6.0 (half the IS→OOS decay) and
> is more consistent. *Caveats:* windows overlap (6mo step) so they are not 45
> independent samples, and this is still full-history walk-forward, not a locked
> holdout (Phase C). This validates the **budget** choice (temporal stability); the
> broader V5 re-tune + robustness + reality-check gate on the headline edge remains.
> **Shipped-default headline is now 25d-hard @ budget 5.0 (`run_id=8`: +87.2R,
> Sharpe 0.58, Sortino 1.03, PF 1.29, 1194 trades).**

### Biases still NOT addressed (the honest gaps)
1. **Survivorship — Phase A (biggest).** `tier_a` is hand-picked; the
   frozen-universe A/B (TODO Phase A1) is still open. The walk-forward does nothing
   for this — if the watchlist itself is hindsight-selected, every number above is
   inflated by an unknown amount.
2. **Parameter-selection — Phase D.** V5b used `--wf-no-retune` (fixed config), which
   tests temporal stability, **not** the data-snooping from tuning the current params
   on full history. The **re-tune walk-forward** (`--walk-forward --workers N`, now
   parallel) plus a deflated-Sharpe / White's-reality-check across *all* configs tried
   is required to close this.
3. **Friction stress — Phase B (measured 2026-06-05, `scripts/friction_sweep.py`).** Slippage
   bites hard. Current universe/data, 25d-hard @ budget 5.0: 0→**+117.3R**/0.72 Sharpe,
   0.0005→+95.9R/0.60, 0.001→+75.6R/0.48, **0.002→+43.8R/0.29**, 0.003→+14.7R/0.10; commission
   is mild (0→0.01 ≈ +48.5R→+39.1R). Defaults raised to a conservative `entry_slippage_pct=0.002`
   (new headline baseline = +43.8R, Sharpe 0.29) and `borrow.annual_rate_default=0.03` (shorts
   only). The edge is thin and **highly slippage-sensitive** — the single biggest win-now risk.
4. **Locked OOS — Phase C.** Tune ≤2015, lock, test 2016–2026 once.

### De-biasing roadmap (system-design view)
```
   in-sample headline  ──►  V5b temporal stability  ──►  re-tune WF + deflated SR
   (+101R, SR 0.59)         (✅ ~0 degradation)           (Phase D — pending)
        │                                                      │
        └──────────►  frozen-universe A/B (Phase A) ◄──────────┘
                      (survivorship — the gating unknown)
```
Order to run: **Phase A first** (it can invalidate everything else cheaply), then
Phase D (re-tune WF), then B/C.

## 4. Debug — issues found & fixed this session

| Issue | Root cause | Fix | Prevention |
|-------|-----------|-----|------------|
| Walk-forward ~5 h | `n_workers` hardcoded 0 + `re_tune=True` ran ~900 sweeps single-threaded | `--wf-no-retune` (≈18 runs) + thread `--workers` into WF | runbook documents both modes |
| `--workers` no speedup on sweeps | 75 MB universe pickled **per job** (≈7.5 GB IPC) | ship once per worker via pool `initializer` | `RuntimeError` guard if uninit |
| `UnicodeEncodeError` on piped runs | Windows cp1252 + Unicode console output | force UTF-8 stdout at entry points | runbook Step 0 sets encoding |
| pytest "failures" in sandbox | mount served stale/truncated files to the importer | n/a (environment) | run pytest on the real machine (197 ✅) |

## 5. Recommendation

Ship the fixes — they're correct, tested, and reversible. Adopt **25-bar hard** as
the *provisional* validation headline (Sharpe ~0.44 deflated; ~0 OOS degradation),
explicitly labelled provisional until **Phase A (survivorship)** and **Phase D
(re-tune WF + multiple-testing haircut)** are run. Those two — not more max-hold
tuning — are the next things that can actually change the conclusion.
