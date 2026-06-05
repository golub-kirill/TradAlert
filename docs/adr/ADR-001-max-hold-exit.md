# ADR-001: Time-based max-hold exit (swing-horizon enforcement)

**Status:** Accepted (implemented, OFF by default) — 2026-06-03
**Deciders:** repo owner
**Related:** TODO.md "Raw notes — Note 1"; `docs/triage_raw_notes_2026-06.md`;
Validation & de-biasing program (Phase B/C)

## Context

The backtester's only exit paths are `engine_exit`, `stop`, `target`, and
`open_eod` (`backtest/portfolio_backtester.py`). There is **no time-based
exit**. `expected_hold_days` (default `(10, 15)`, `filter_engine.py:201`) is a
*reported* field only — never enforced. So a held trade can run until it tags a
stop or target, however long that takes, while the live thesis is a ~10–15-day
swing. Backtest and live therefore run on **different exit logic**, and the
reported win rate is measured on a horizon the strategy would never actually
trade.

The existing baseline ledger (`data/backtest_out/trades.csv`, 1,116 trades)
makes the bias concrete:

| Cohort | Trades | Win rate | Total R |
|--------|--------|----------|---------|
| Held **≤ 15 bars** | 798 | **41.5%** | **−52.1R** |
| Held **> 15 bars** | 318 | 57.5% | **+175.0R** |
| Held > 20 bars | 220 | 62.3% | +155.6R |
| Held > 30 bars | 118 | 62.7% | +92.7R |
| **All** | 1,104 | 46.6% | +128.1R |

Exit-reason view: `target` exits (the big winners, +178.8R, 100% WR by
construction) have a **median hold of 27 bars**.

Reading: essentially **the entire net edge lives in the >15-bar tail**; the
≤15-bar cohort — the part that actually fits the swing horizon — is net
*negative*. The headline +128R / 46.6% WR is, in large part, an artifact of
holding winners far longer than the stated strategy would.

This does not by itself prove the strategy has no edge — but it proves the
*reported* number answers the wrong question. Enforcing the swing horizon is
expected to **shrink** the headline, which is the explicit goal of the
validation program ("shrink the headline until only the trustworthy part
remains").

## Decision

Add an **opt-in, time-based max-hold exit** to both backtesters. When
`max_hold_days` is set, a still-open trade is force-closed at the bar's
**close** once it has been held that many trading bars (new exit reason
`time_stop`). It is **OFF by default** (`max_hold_days = None`), so the existing
baseline replays bit-identically — consistent with every other strategy flag in
the project.

Two modes, because the ledger shows the choice is consequential, not cosmetic:

- **`hard`** (default): always exit at the cap. Produces the honest "pure swing"
  number. Expected to cut the +175R tail hard.
- **`if_not_profit`**: exit at the cap only when the position is *not* in profit
  at that close; let winners run to target. A middle path that cuts dead/losing
  long holds while preserving maturing winners.

**Canonical mode for the validation program: `hard`** (decided 2026-06-03). The
validation program should report the most conservative, swing-consistent number;
`hard` is that number (it makes no "let winners run" concession). `if_not_profit`
is a documented lever to switch on *after* the program has fully validated the
edge under the strict assumption — not before.

**Horizon: 25 trading bars** (decided 2026-06-04) — the best risk-adjusted point in
the ≤30 range (Sharpe 0.48 vs 0.44 at 30d; see Verification results). Applied via
`--max-hold-days 25 --max-hold-mode hard`; kept **flag-driven, not a YAML default**,
until the walk-forward + robustness gate (V5) passes.

Same-bar precedence: stop and target are checked first (the house pessimistic
convention), so a trade that also hits its stop on the cap bar records the
stop, not the time-stop.

Bar-count convention matches `Trade.bars_held` (`exit_idx − entry_idx`): with
`max_hold_days = 15`, a trade entered at T+1 closes once it has been held 15
bars.

## Options Considered

### Option A: Hard time-stop only, fill at bar close *(chosen core)*

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — one branch in Phase 3, self-contained |
| Realism | Good — equivalent to a market-on-close after N days |
| Baseline safety | High — gated on `max_hold_days is not None` |

**Pros:** simplest; deterministic; directly answers "what's the edge on a real
swing horizon."
**Cons:** blunt — kills winners that need >N bars to mature (the ledger says
those carry the edge), so used alone it may flip the strategy flat/negative.

### Option B: Add the `if_not_profit` mode alongside A *(also chosen)*

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — one extra predicate (`in_profit`) |
| Realism | Good — "time-cut losers, ride winners" is a real discipline |
| Risk | Re-introduces an unbounded hold for winners (partial reversion to the bug) |

**Pros:** preserves the maturing-winner edge while removing stale/losing holds;
lets us A/B the two philosophies.
**Cons:** "let winners run" still permits very long holds, so it only partially
restores swing-consistency; needs its own validation.

### Option C: Fill at next bar's open (T+1), like engine exits

| Dimension | Assessment |
|-----------|------------|
| Complexity | Higher — needs a pending-exit state + EOD-boundary handling |
| Realism | Comparable (MOO vs MOC) |

**Pros:** uniform with `engine_exit` fill geometry.
**Cons:** more state, more edge cases at end-of-timeline, no measurable accuracy
gain. **Rejected** for v1.

### Option D: Do nothing / only report hold-length

**Rejected** — leaves the headline WR measuring the wrong horizon.

## Trade-off Analysis

The core tension is *honesty vs. edge*. `hard` gives the most defensible number
but likely removes most of the reported edge; `if_not_profit` keeps more edge
but is a weaker form of swing-consistency. Rather than pick for the owner, both
are shipped behind one flag so the difference can be **measured** against the
same universe — which is the right input to the validation program. Fill-at-
close (A/B) was chosen over fill-at-open (C) purely to minimise blast radius and
new state; it can be revisited if reconciliation against live fills warrants it.

## Results (measured 2026-06-03)

Full-history replay (75 tickers, 2000–2026), 30-bar cap, both modes, vs. the
uncapped baseline (prior ledger: ~1,116 trades, 46.6% WR, +128.1R). Runs
journaled as `backtest_runs` 4 (hard) and 5 (if_not_profit).

| Metric | Baseline (no cap) | 30d **hard** | 30d if_not_profit |
|--------|-------------------|--------------|-------------------|
| Trades | ~1,116 | 1,170 | 1,121 |
| Win rate | 46.6% | 47.5% | 45.8% |
| Expectancy (R) | — | +0.072 | +0.098 |
| **Total R** | **+128.1** | **+83.9 (−34%)** | **+109.4 (−15%)** |
| Profit factor | — | 1.27 | 1.37 |
| Sharpe (monthly) | — | 0.47 | 0.64 |
| Calmar | — | 0.10 | 0.14 |
| MC p95 drawdown | — | 20.0R | 17.7R |

**The `time_stop` cohort is the diagnostic** (what the cap actually cut):

- `hard`: 126 time-stops, **82% WR, +0.573R** — it cuts *winners* still drifting
  toward target at bar 30 (target exits fell 70 → 47). Blunt but honest.
- `if_not_profit`: 46 time-stops, **0% WR, −0.244R** — cuts *only* dead/losing
  holds; winners preserved (target exits rose 70 → 75). Works exactly as designed.

This also empirically confirms the implementation in a real (non-sandbox) run:
the `time_stop` reason fires and the per-mode behavior is exactly as specified.

Conclusions: (1) the edge is **not** purely a long-hold artifact — both capped
variants stay positive with bootstrap CIs excluding zero (hard +33→+136R, soft
+57→+163R); (2) `if_not_profit` dominates `hard` on every risk metric, but per
the Decision above, **`hard` is the canonical validation number**; (3) 30 bars is
lenient vs. the ~10–15-day thesis — the 15-bar run is still pending (use
`scripts/compare_max_hold.py`) and is expected to haircut further.

## Verification results (2026-06-04, hard mode)

`pytest tests/` **green (197 passed)** — all 4 max-hold tests + the two new
ticker-health tests pass. Correctness confirmed against the raw ledgers: the OFF
run has no `time_stop` and max `bars_held` = **129** (unbounded); the capped runs
hold **every** exit type to the horizon (max `bars_held` = cap) with `time_stop`
firing exactly at the cap. The exit is airtight and OFF-by-default is intact.

Hard-mode horizon sweep (effective-R, full history; uncapped baseline = +101.3R,
Sharpe 0.59):

| Horizon | Total R | Sharpe | PF | Avg held |
|---------|---------|--------|----|----------|
| 10d | +54.6 | 0.31 | 1.18 | 7.2 |
| 15d | +66.0 | 0.37 | 1.21 | 9.0 |
| 20d | +77.3 | 0.46 | 1.24 | 10.2 |
| **25d (headline)** | **+83.4** | **0.48** | 1.27 | 11.1 |
| 30d | +81.5 | 0.44 | 1.26 | 11.7 |

Edge stays positive (PF > 1) at every horizon → not purely a long-hold artifact.
**25 bars chosen** as the headline (best risk-adjusted ≤30). The honest swing edge
is ~20% below the uncapped baseline in R and ~0.1 lower in Sharpe.

Chronic-penalty A/B (de-fanged scale `{2: 0.5, 3: 0.25}`, OFF→ON): Total
+101.3→+102.3R, Sharpe 0.59→0.60, Calmar 0.12→0.13, max-DD 31.2→29.3R (−1.9),
trades/WR unchanged. Clears the keep rule (risk-adjusted up, DD down, no R cost),
but the effect is small/near-noise — keep ON as a mild variance damper, don't lean
on it.

> Note: raw `r_multiple` ledger sums (e.g. OFF = +132.9R) differ from the
> effective-R figures above (size-scaled by macro×behavioral×size-gate). Decisions
> use effective-R (report / compare / A/B); the raw ledger is for correctness checks.

**Pending gate — V5:** walk-forward (OOS) + robustness on 25d hard, to confirm the
shrunken edge isn't itself overfit, before it becomes the validation headline.

## Consequences

- **Easier:** measuring the strategy on its actual swing horizon; the
  `time_stop` reason flows automatically into the existing By-Exit breakdown and
  stats (grouped dynamically on `exit_reason`).
- **Harder / to revisit:** the headline edge will drop when enabled — downstream
  numbers (expectancy, equity curve, Kelly) must be re-read with the flag on,
  not compared to the old baseline. `if_not_profit` still allows long winner
  holds, so it is not a full swing-consistency guarantee.
- **Live alignment is out of scope here.** This fixes the *backtest*. Making the
  live/position side enforce the same horizon (e.g. `position_CLI` auto-close
  after N days) is a separate follow-up.

## Implementation (landed, OFF by default)

- `Trade.ExitReason` += `"time_stop"` (`backtest/trade.py`).
- `PortfolioConfig` / `BacktestConfig` += `max_hold_days: Optional[int] = None`,
  `max_hold_mode: str = "hard"`.
- Phase-3 time-stop branch added to `run_all`, `run_prepped`
  (`portfolio_backtester.py`) and `_walk` (`backtester.py`) — after stop/target,
  fill at close.
- Sweep allowlist `_PORT_FIELDS` += the two keys (`sweep.py`) so they reach
  worker `PortfolioConfig`s.
- CLI `--max-hold-days N` / `--max-hold-mode {hard,if-not-profit}`
  (`run_backtest.py`); default source `execution.max_hold_days` /
  `execution.max_hold_mode` in `filters.yaml` (documented, commented OFF).
- Test: `tests/test_max_hold_exit.py` (hard cut at cap, baseline unchanged when
  off, `if_not_profit` rides a winner, `if_not_profit` still cuts a flat trade).

## Action Items

1. [x] `pytest tests/` green — 197 passed (2026-06-04).
2. [x] Measure 30-bar cap, both modes (2026-06-03) + full hard 10→30 curve
       (2026-06-04) — see Verification results.
3. [x] Headline picked: **25 bars, hard**.
4. [x] Mode decision: **`hard` is canonical**; `if_not_profit` deferred.
5. [ ] **V5 (next):** walk-forward + robustness on 25d hard — confirm the edge
       survives OOS before adopting it as the headline.
6. [ ] After V5 passes: fold the 25d-hard number into Phase B/C as the headline,
       retire the uncapped baseline, and consider setting `execution.max_hold_days:
       25` as the config default (it is flag-driven until then).
7. [ ] (Later) Align live/position exit logic to the 25-bar horizon.
