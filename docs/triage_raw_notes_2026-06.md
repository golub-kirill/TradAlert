# Raw Notes Triage — TODO.md "(from the sweep output)"

Date: 2026-06-03. Status: **plan only, no code changed.** Awaiting sign-off.

Triage of the four terse notes under the unlabeled top section of `TODO.md`. Each
note was traced into the actual code before classifying. Headline: only **one** of
the four (Note 1) is a real correctness bug; two are "inert, not broken" (the params
*are* wired, they just don't move results, for two different reasons); one is a
cosmetic convention question; the feature in Note 4 already largely exists.

## At a glance

| # | Note (verbatim) | What it actually is | Severity | Next action |
|---|-----------------|---------------------|----------|-------------|
| 1 | days to hold → backtest = dead target 100% | **Bug:** no max-hold exit; backtest holds 30+ days while live thesis is swing → WR is artificial | **High** | Add optional max-hold-days exit; re-measure WR |
| 2a | "Momentum fade RSI floor" identical regardless of value | **Not a wiring bug** — param is read but the RSI floor rarely *binds* at a fade | Low | Instrument; prune or widen the swept range |
| 2b | "Breadth divergence pen." identical | **Wired-but-dormant** (behavioral IS live in the sweep; my first pass was wrong). Penalty only bites when a breadth divergence fires — rarely | Low | No fix; instrument divergence frequency (comment added) |
| — | (found while checking 2b) "Behavioral size floor" | **Real dead key — FIXED:** swept `size_multiplier_floor`; consumers read `size_mult_floor` | Low–Med | Renamed ParamSpec + alias to `size_mult_floor` ✅ |
| 3 | why do only some report numbers have colors | **Cosmetic — FIXED:** ad-hoc coloring, no convention | Low | One convention applied (`_pnl_color` + `_er_color`) ✅ |
| 4 | Consecutive-loss guards in the backtest | **Already built** (`TickerHealth`); weak-but-real signal, block-at-4 unjustified | Low | Scale de-fanged to `{2:.5, 3:.25}` ✅; A/B pending (`ab_chronic_penalty.py`) |

A cross-cutting theme: Notes 1, 2a, 2b are all variants of *"the backtest is silently
measuring something other than what you think."* That is exactly the question the
**Validation & de-biasing program** (next section of TODO) exists to answer, so these
should fold into it rather than be treated as isolated chores — Note 1 in particular
feeds the "is the +96.2R edge real" question because it touches headline WR.

---

## Note 1 — "days to hold → backtest = dead target 100%"

**What it actually is.** The backtester has four exit paths only —
`engine_exit` (signal), `stop`, `target`, `open_eod` (end-of-data) — see
`backtest/portfolio_backtester.py:369,483,498,575,750,763,826`. There is **no
time-based / max-hold exit anywhere.** `expected_hold_days` exists solely as a
*reported* field (`src/core/filter_engine.py:201`, default `(10, 15)`) and is never
enforced. So a position can be held 30+ days (confirmed against the SQL journal),
even though the live strategy is swing-trading with a ~10–15-day horizon.

**Why it matters.** Backtest and live run on *different exit logic*. A trade that
would have been time-stopped at day 15 in live trading is instead held in the
backtest until it eventually hits its target — converting would-be scratches/losers
into wins. The reported **win rate is therefore an artificial number**, biased
upward, and it propagates into expectancy, the equity curve, and the validation
program's headline. This is the only note in the set that corrupts results.

**Classification:** correctness / methodology bug. **Severity: High.**

**Recommended action.**
1. Add an opt-in `max_hold_days` exit to the backtester (new exit reason
   `time_stop`), defaulting **OFF** so the existing baseline replays bit-identically
   (consistent with the project's strategy-flag convention).
2. Quantify the bias *before* changing defaults: count trades exceeding 15 / 20 / 30
   trading days and report their WR and R contribution, so we see exactly how much of
   the headline WR depends on over-long holds.
3. Decide the live-consistent horizon (likely `expected_hold_days.high`), then
   re-run baseline with the time-stop on and report the WR delta.
4. Fold the corrected WR into the validation program as the trustworthy number.

**Effort:** Medium. **Risk:** low (additive, gated off by default).
**Open question:** exact max-hold value — `expected_hold_days.high` (15), or a
separate configurable? And: hard exit at the bar, or only-if-not-in-profit?

---

## Note 2 — sweep params showing identical results

Your note hypothesizes one cause ("the swept param isn't being applied — sweep wiring
bug"). After tracing both: **the wiring is fine in both cases.** The values *are*
written into config and *are* read by the engine. They fail to move results for two
*different* downstream reasons, and the fix differs accordingly.

### 2a — "Momentum fade RSI floor" (`signals.momentum.short.rsi_min`)

**What it actually is.** This key is read by `_momentum_fade_exit`
(`src/core/filter_engine.py:1025`), the **held-long momentum-fade EXIT** (the
`signals.momentum.short` name is legacy — it is *not* a short entry here). The exit
fires only on a MACD histogram zero-cross-down **and** `rsi_min ≤ rsi ≤ rsi_max`.
At the moment momentum fades from a winning long, RSI is almost always well above the
swept floor (25–40), so the **lower bound rarely binds** — the MACD cross and the
upper bound dominate. Sweeping the floor 25→40 changes (almost) nothing because the
constraint is economically inert, not because it is unwired.

**Classification:** inert sweep dimension (no defect). **Severity: Low.**

**Recommended action.** Instrument one sweep run to count how often `rsi < rsi_min`
is the binding reason a fade is rejected. If near-zero (expected), either drop this
row from `PARAM_GRID` or widen the range so it actually binds — and add a one-line
comment so the next reader doesn't re-file this as a bug. Also worth a docstring note
that `signals.momentum.short` = the held-long fade exit, since the name invites
exactly this confusion.

**Measured (2026-06-05, `scripts/instrument_binds.py`).** Over the 91-ticker watchlist:
429 fade-eligible bars (MACD cross-down + magnitude gate). The `rsi_min=30` floor withholds
36 of them (8.4%); the `rsi_max=65` ceiling withholds 0 (0.0%). So the **floor is an active
predicate, not inert** — the "rarely binds" hypothesis above was wrong. The sweep's flatness
is therefore exit-substitution (a withheld fade defers to stop / max-hold / a later fade),
not an inert gate. Keep the floor; pruning the sweep row is still defensible as "non-moving",
but not on inert-gate grounds. (The 8.4% is unconditional over all bars, not just held-long
bars, so it is an upper bound on P&L impact.)

**Resolved 2026-06-05.** Floor KEPT (active predicate, not inert); sweep row unchanged.

### 2b — "Breadth divergence pen." (`behavioral.breadth_divergence_penalty`)

**What it actually is.** The key is read by the behavioral sizer
(`src/core/behavioral/__init__.py:230`). **Correction (verified 2026-06-03 — my first
pass was wrong here):** the behavioral layer **is live in the sweep**.
`data/behavioral/*.parquet` (7 datasets) is loaded by `load_universe`,
`SweepEngine._run_one` passes `behavioral_data` *and* `settings` into `run_prepped`
(`backtest/sweep.py:799-801`), and `_SETTINGS_ALIASES` routes this key to the exact name
the classifier reads. So it is wired end-to-end. (I originally checked the
`run_backtest`/`run_all` default signature — where macro/behavioral default to `None` —
and wrongly concluded the layer was off; the real replay path is via `SweepEngine`.)

The penalty only subtracts from `behavioral_score` **when a breadth divergence is
flagged**, so "identical results" means that condition rarely/never fires over the window
— the penalty is **dormant, not unwired**. Same shape as 2a: a correctly-wired gate that
seldom binds.

**Classification:** wired-but-dormant (no defect). **Severity: Low.**

**Recommended action.** No code fix. Instrument how often the breadth-divergence flag is
True across the backtest (and, when True, whether the resulting `size_mult` change flips
any entry via `size_mult_gate`). If it genuinely never fires, that's a data/threshold
question for the behavioral layer, not a sweep bug. A clarifying comment was added at the
ParamSpec so it isn't re-filed as a wiring bug.

**Measured (2026-06-05, `scripts/instrument_binds.py`).** Two findings — both correct the
"wired-but-dormant" conclusion above:
1. **Wiring defect, not dormancy.** `loader.load_universe` keys behavioral parquet by stem
   (`sp500_breadth`, `sector_ratios`), but `classify_behavioral_state` reads `breadth` /
   `sector_rotation`, and nothing remaps between them (`portfolio_backtester` passes the dict
   straight through at `:369`/`:694`). So breadth is **missing in every backtest/sweep** —
   `breadth_divergence` is structurally False regardless of the penalty. The 2026-06-03
   "wired end-to-end" note routed the penalty *value* but missed that the *data* never
   arrives. (The live path is fine: `fetch_all_behavioral` emits `breadth`/`sector_rotation`.)
2. **Inert even if fixed.** With correct keys the flag fires on 6 / 6833 days (0.09%) over
   1999–2026, last on 2015-10-19. So the penalty can't move results either way — the sweep
   row can be pruned. Fixing the loader key is a separable live/backtest-fidelity correctness
   fix (negligible P&L: ~6 historical days).

**Resolved 2026-06-05.** Both actions landed: (1) `loader._BEHAVIORAL_KEY_ALIASES` remaps
`sp500_breadth`→`breadth` and `sector_ratios`→`sector_rotation`, so backtests now feed the
classifier the same key contract as live (breadth, the weight-4 axis, and sector are no
longer NEUTRAL-pinned). (2) The `breadth_divergence_penalty` PARAM_GRID row was pruned (inert).
NB the key fix changes the sweep's behavioral axes — re-run behavioral sweep rows if they are
tuned for a headline config.

### Bonus finding — "Behavioral size floor" was a genuine dead key (FIXED 2026-06-03)

`PARAM_GRID` swept `behavioral.size_multiplier_floor` and `_SETTINGS_ALIASES` routed it to
the *same wrong name*, but every consumer/default reads **`size_mult_floor`**
(`src/core/behavioral/__init__.py:235`, `src/core/defaults.py:46`). So the swept value
landed on a key nobody reads → the "Behavioral size floor" row had no effect. **Fix:**
renamed the ParamSpec dotted and the alias to `behavioral.size_mult_floor`
(`backtest/sweep.py`). Because behavioral is live in the sweep, this row now actually
varies the floor (0.25 → 0.65) — no further wiring needed. (This corrects my earlier note
that it was only observable "once behavioral wiring lands" — it was already wired.)

---

## Note 3 — "why do only some report numbers have colors"

**What it actually is.** Coloring in `backtest/report.py` is applied ad hoc, section
by section. Some numbers route through `_er_color` / `_GREEN` / `_RED` (lines 44,
106, 168, 182–186, 214); others print with no color (`_RESET` / plain). There is no
shared rule for *which* numbers get color — that inconsistency is the entire answer.

**Classification:** cosmetic / consistency. **Severity: Low.**

**Resolution (2026-06-03 — FIXED).** One convention adopted and documented in
`report.py`: sweep/comparison tables colour a metric **relative to baseline**
(`_er_color`); standalone P&L figures colour by **absolute sign** (`_pnl_color`,
pivot 0 for signed R, 1.0 for profit factor); descriptive/structural figures
(trade counts, win rate, dates, drawdown magnitude, avg bars held, parameter
values) stay **uncoloured**. Applied to the previously-plain spots: the baseline
headline block (expectancy, total R, profit factor, best/worst), the
By-signal/regime/exit/year breakdown E[R], and the Kelly edge-per-trade. The
sweep tables already used `_er_color` and were left as-is. Terminal/ANSI only
(`_USE_COLOR = os.isatty(1)` → no codes when piped); the HTML report colours via
CSS on a separate path.

---

## Note 4 — "Consecutive-loss guards in the backtest"

**Verification result (you asked me to check whether this is a duplicate).**
A per-ticker **consecutive-loss guard already exists**: `src/core/ticker_health.py`
(`TickerHealth`) tracks consecutive losses per symbol within a rolling window and
applies a sliding-scale size penalty — streak 2 → ×0.5, 3 → ×0.25, **≥4 → ×0.0
(hard block)**. It is the `chronic_loser_penalty` / `--chronic-penalty` feature and
is wired into both backtesters (`portfolio_backtester.py:419`, `backtester.py:373`).
Separately, a portfolio **drawdown circuit-breaker** exists (`max_drawdown_r`). So
the note is **largely redundant** with code already in the tree — likely the coding
model re-suggested something already built.

**What does *not* exist:** an **account-wide** consecutive-loss cooldown (pause *all*
new entries after N consecutive losing trades across the portfolio, resume after a win
or a cooldown window). That is the only genuinely new variant.

**Classification:** feature (mostly pre-existing). **Severity: Low (no bug).**

**Recommended action.**
1. Confirm the per-ticker `TickerHealth` behavior above is *not* what Note 4 was
   asking for; if it is, close the note as already-done.
2. If you specifically want the **portfolio-level** cooldown, treat it as a new edge
   experiment: short PRD (problem, success criterion = does it lift risk-adjusted
   return after costs, scope, off-by-default flag), then A/B it. Per your operating
   rules, no build before that PRD and sign-off.

**"Reasonable or eye sugar?" — analysis (2026-06-03).** Tested whether per-ticker
losing streaks actually predict the next trade (penalty was OFF, so streaks are
natural). Forward outcome by exact prior-loss streak:

| Prior consec. losses | Next WR | Next mean R | n |
|----------------------|---------|-------------|---|
| 0 | 46.4% | +0.148 | 550 |
| 1 | 48.0% | +0.126 | 273 |
| 2 (½ size) | 44.4% | +0.113 | 135 |
| 3 (¼ size) | 41.2% | +0.001 | 68 |
| **4+ (was blocked)** | 41.1% | **+0.112** | 90 |

Regime-control: streaks are barely over-represented in the worse regime (BULL_LOW
88% vs 86% base), and **within** BULL_LOW the effect survives (44.2% → 39.8% after
≥2 losses, n=259). So it is *not* purely a regime proxy — a genuine but weak
per-ticker continuation effect (within ~1 SE). Caveat: only bull regimes are
traded, so the confound is untestable until bear/shorts are live.

**Verdict:** reasonable premise, over-tuned response. The hard **block at 4 was
unjustified** — forward EV there is still **+0.11R**. EV stays positive even after
2 losses, so downsizing is defensible but eliminating is not.

**Done (config-only):** `filters.yaml` scale de-fanged to `{2: 0.5, 3: 0.25}` —
4+ floors at 0.25, no block. **Pending:** `scripts/ab_chronic_penalty.py` runs
baseline vs penalty-ON and judges on **Sharpe / Calmar / max-DD** (the penalty is
a variance tool, not an edge source — keep only if it improves risk-adjusted
return). Account-wide cooldown remains unbuilt (PRD first if wanted).

---

## Recommended sequence

1. **Note 1 (max-hold exit)** — highest value, fixes a real WR bias, feeds the
   validation program. Do first.
2. **Note 2b + bonus dead key** — stop the sweep from emitting silent "no-effect"
   rows: either wire behavioral in (with look-ahead care) or prune phase-8 + fix the
   `size_mult_floor` name. Medium.
3. **Note 2a** — cheap instrumentation, then prune/relabel + add the legacy-name
   comment. Small.
4. **Note 3** — apply one coloring convention; batch with the next `report.py` edit.
   Small.
5. **Note 4** — close as already-implemented, *or* spin a PRD for the account-wide
   cooldown if you actually want it. No build until then.

## Open questions for sign-off

- **Note 1:** max-hold value (15 = `expected_hold_days.high`, or configurable?), and
  hard time-exit vs. exit-only-if-not-in-profit?
- **Note 2b:** wire behavioral into the sweep (more work, but makes phase-8 real), or
  prune those rows for now?
- **Note 4:** is `TickerHealth` already what you meant, or do you want the new
  account-wide cooldown?
