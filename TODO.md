# TODO

> ## ★ NORTH STAR #1: WIN NOW — the backtest is secondary.
> The strategy must be **profitable in the current market**. A config that maxes the
> 2001+ backtest but is **losing today is wrong**. Evaluate/tune on **recent & current
> reality** (regime/behavioral/size-mult adaptivity, recent windows, live signals +
> open positions) — not 25-year aggregates. Deep validation (survivorship, walk-forward
> rigor) is **secondary** until live performance is healthy.
>
> ## ★ NORTH STAR #2: UNIVERSE-AGNOSTIC — don't tune to the watchlist.
> The watchlist is an input that **changes** (a handful or hundreds). Logic and parameters
> must hold across **any size/composition** — never overfit to the current ~213 names or
> their count. Prefer **relative/percentile/adaptive** knobs over absolute counts. (Known
> offenders fixed: `max_concurrent`→`max_open_risk` budget, breadth now full-universe —
> keep the principle in mind for any new knob.)

---

**Current headline:** `run_id=11` — 213-name universe, 25d `if_not_profit`, budget 5.0,
slippage 0.002, **scoring OFF**: +116.7R, Sharpe 0.66, PF 1.30, E[R] +0.075 (ADR-003).

---

## ▶ NEXT CHAT — recommended next move (start here)

**State (2026-06-08):** The **Phase-2 interactive Telegram daemon (`telegram_bot.py`) is built + tested**
on `v3-release` (working tree — **commit pending**) — owner-only PTB long-poll, single-instance lockfile
(`data/telegram_bot.lock`), the alert/position buttons + commands (`/positions /pos /recalc /open
/close /stop /status /chart /scan /help`), every mutation through the broker-adapter seam, `/close`
behind a Yes/No confirm. `pytest tests/` green at **346** (+8 `tests/test_telegram_bot.py`). Earlier
this cycle (HEAD `cd2646a`): trigger panel + ALL P1 audit fixes + data-driven expected-hold. **Telegram
Phase-1 push is LIVE and ON** (`enabled/daemon_enabled/send_stand_down: true`; token + `TG_CHAT_ID` in
`config/secrets.env`). No sweep/WF running → engine edits are safe.

**▶ NEXT MOVE — run the daemon live + start the paper-fill on-ramp.** The buttons are now wired:
1. **Stop the other poller first** (whatever is draining this bot's `getUpdates` — else Telegram 409
   Conflict), then `python telegram_bot.py` (or `scripts/register_telegram_bot.ps1` for at-logon,
   auto-restart). Tap **📈 Log opened** on an entry card to journal a position → `/positions` to manage
   → **Close** (confirm) to exit → `python scripts/reconcile_fills.py` reads the live edge. This feeds
   the "log real/paper fills so the live meter has data" item (the `positions` table is still empty).
2. **Telegram — richer/prettier messages** (user request, still open): iterate `format.py` templates
   (unicode ▰▱ bars for R:R & distance-to-stop, PnL gauge, section dividers, `<blockquote expandable>`,
   tiered emoji, maybe a MarkdownV2 variant). HTML-safe, caption ≤1024, keep tests de-tagged.

**Secondary (optional rigor, per NORTH STAR #1):** the V5 re-tune walk-forward + Phase-D (below). It
was attempted twice this cycle and **orphaned both times** (agent-spawned background process cut off
over an idle gap — NOT OOM/code error). Run from a terminal that stays open; it only de-biases an
already-thin-but-OOS-passing headline, so it's behind live/paper-trading.

**Done this session (2026-06-08) — on the working tree, commit pending:**
1. ☑ **Phase-2 interactive Telegram daemon — BUILT + TESTED** (2026-06-08). New repo-root `telegram_bot.py`:
   PTB v22 `Application` long-poll, `run_polling(stop_signals=None, drop_pending_updates=True)` (Windows),
   single-instance lockfile via `msvcrt.locking` on a held handle (auto-released on crash → no stale
   lock), **owner-only** via a uniform `_owner_only` decorator on every command + the callback router
   (checks `effective_user.id == TG_CHAT_ID`; `CallbackQueryHandler` has no chat-filter param). Answers
   the Phase-1 buttons (`open:`→`adapter.open` + disarm buttons, `chart:`→fresh render) and position-card
   buttons (`chartpos:`/`stop:`/`close:`/`recalc:`/`confirm:`/`cancel`); `close` gated behind a Yes/No
   `confirm:`. Commands `/positions /pos /recalc /open /close /stop /status /chart /scan /help` reuse
   existing fns — `/recalc` re-reads latest bars and runs the engine exit-check + time-stop (read-only),
   `/chart` re-renders fresh, `/scan` subprocesses `main.py`. Blocking DB/engine/chart calls in
   `asyncio.to_thread`; chart renders serialized behind an `asyncio.Lock` (matplotlib not thread-safe).
   All mutations through `core.execution.adapter` (journal only). Added `position_manager.get_position(id)`;
   extended `mask_api_keys_filter` to mask the bot-token shape (`digits:base64ish`). Deploy:
   `scripts/register_telegram_bot.ps1` + `scripts/run_telegram_bot.bat` (at-logon, auto-restart).
   `tests/test_telegram_bot.py` (+8: parse_callback, owner-reject-no-mutation, `/close` confirm gate,
   `cb_open` delegation, build smoke, token masking) → suite **346 green**. **Not yet run live** (must
   stop the other poller first — 409 Conflict).

**Done previous session (2026-06-07) — committed + pushed to `origin/v3-release`:**
1. ☑ **Entry-gate "trigger panel" — SHIPPED** (2026-06-07). `GateCheck` + `SignalResult.checks`
   built post-decision behind `signal(with_checks=True)` (default OFF → backtest byte-identical, zero
   extra compute; `with_checks=True` set only in `main.py`). New `_build_gate_checks` re-derives
   direction-aware factor groups (TREND/MOMENTUM/LOCATION&STRENGTH/VOLATILITY/RISK/CONTEXT) from
   already-loaded data (incl. `market_dfs["SPY"]` RS, VBP, BB %ile). Chart `_render_trigger_panel`
   renders grouped rows + graded `●●●○` / `✓`·`✗`; Telegram factor line lit up (`push._checklist`).
   `main.py` folds in live RP + open-count; added `vbp.nearest_high_volume_node_below`.
   `tests/test_trigger_panel.py` (+11, incl. replay-equality).
2. ☑ **P1 engine bug fixes — ALL DONE** (2026-06-07, pytest-gated). Each verified at the code, fixed,
   and covered by a test (`tests/test_p1_fixes.py` +14, `tests/test_reconcile_provenance.py` +6,
   trigger-panel +2). Suite **330 green**. Closed: live `max_open_risk`+`size_mult` surfacing
   (main.py report + trigger panel + `risk.max_open_risk` setting); behavioral `missing_axes` canonical
   names + non-negative confidence; WF degradation now tuned-IS-vs-tuned-OOS + 20-trade IS floor; macro
   `regime.py` (12-mo inflation YoY, fresh-inversion→INVERTED, credit ≥1yr date-span guard, WCS
   Brent−WTI sign reachable); profit-factor r==0 neutral (unified with stats_utils); negative-caching
   (cache market-cap/live-price on success only); `backtest_trades` gains `effective_r`/`size_mult`/
   `borrow_annual_rate` (resilient writer + reconcilers aggregate effective_r); reconciler provenance
   (prefer latest scoring-OFF run via `_meta.use_scoring`) + COALESCE signal_type both sides;
   `reconcile_live._replay` max-hold parity test; dead code removed (`run_all`, `--earnings-aware`,
   `_quarantine`, `:317` ternary). **Deferred (polish, not a bug):** extract a shared `_maybe_time_stop`
   (the time-stop DECISION already shares `core.exits.max_hold_exit_due`; only boilerplate is duplicated).
3. ☑ **Expected-hold made data-driven** (2026-06-07) — the "hold N–Md" caption is now the p25–p75 of
   real `backtest_trades.bars_held` (`backtest.db.expected_hold_range`, no upper clamp — `if_not_profit`
   winners run past the cap), set on every fired entry **regardless of scoring**; killed the four
   disagreeing sources. run_id=12 → **(3, 15)**, avg 11.2, max 122. `tests/test_hold_range.py` (+8).

**Deferred — optional rigor (not blocking; secondary per NORTH STAR #1):**
- ◻ **V5 re-tune walk-forward + Phase-D.** `python backtest/run_backtest.py --walk-forward --workers 8`
  — run it from a **terminal that stays open** (or detached via `Start-Process`); the agent-spawned
  background process gets orphaned over idle gaps, so don't launch it from the agent. Then
  `python scripts/multiple_testing.py --workers 8` → update `verification_results` + README. The
  degradation metric is fixed (tuned-IS vs tuned-OOS + trade floor) so the verdict is trustworthy when
  run; it only de-biases the already-thin headline (the fixed-config OOS already PASSED, `run_id=12`:
  68% OOS, p≈0.009), so it's secondary to live/paper-trading.

**Telegram — richer/prettier/creative messages (user request, STILL OPEN).** Phase-1 ships a clean
`<blockquote>` card (`src/core/telegram/format.py`), but the user wants a **more visually rich,
creative** look — iterate the templates: e.g. unicode progress/▰▱ bars for R:R & distance-to-stop,
mini PnL gauge, tiered/section dividers, `<blockquote expandable>` for detail, smarter emoji tiers,
maybe a MarkdownV2 variant. Stay HTML-safe (caption ≤1024, escape values) and keep tests de-tagged.
The **Phase-2 interactive daemon** `telegram_bot.py` is now **SHIPPED** (see Done this session) — these
template tweaks light up on both the push and the daemon's cards (one source: `format.py`).

---

- ☑ **Fixed-config OOS validation PASSED** (2026-06-06, `run_id=12`). `--wf-no-retune --workers 14`:
  47 windows, IS +0.066 → OOS +0.072 (degradation −0.006), **68% OOS-profitable, p≈0.009**. The
  scoring-OFF headline is temporally stable (recorded in `verification_results`).
- ◻ **Run the FULL re-tune walk-forward (V5)** over the whole range (`--walk-forward --workers 14`,
  no `--wf-no-retune`) — the parameter-selection / data-snooping test the fixed-config WF does NOT
  cover. Then deflated-Sharpe / White reality check (Phase D). This is what's left to fully de-bias
  the +0.075 / Sharpe 0.66 headline.
- ◻ **Real-life "textbook" check (SPY benchmark built; result sobering).** `scripts/benchmark_spy.py`
  (+`tests/test_benchmark_spy.py`) computes passive SPY buy-and-hold risk-adjusted metrics over
  full/10y/5y/3y/1y. **Strategy Sharpe 0.66 vs SPY full-window 0.62 (+0.04 only); SPY BEATS it in every
  recent window** (10y 1.00, 5y 0.87, 3y 1.55, 1y 1.70) — i.e. passive has been the better risk-adjusted
  bet lately (NORTH STAR #1 red flag). Caveat: that compares strategy *full-history* Sharpe vs SPY
  *per-window*. **Next:** run the strategy over trailing 1y/3y/5y for an apples-to-apples per-window
  Sharpe vs SPY (post-V5, needs workers), fold into `verification_results`, and hand-work one known
  trade (entry/stop/target → realized R) to confirm the backtester reproduces it.
- ◻ **Let the live feed mature.** Scheduling is done — `scripts/register_daily_scan.ps1`
  registers a Task Scheduler job (`main.py` Mon–Fri 18:00 local, only-when-logged-on,
  catches up missed runs). Now it just needs calendar time: signals mature in ~25 trading
  days, so the meaningful read on "winning now" is ~5 weeks of daily runs out.
- ◻ **Log real/paper fills so the real meter has data.** The reconciler is built
  (`scripts/reconcile_fills.py` — realized R on closed `positions` vs `backtest_trades` by
  direction) and `position_CLI.py open --date` backfills retroactive opens. `positions` is
  still **empty**, so the remaining work is operational: log actual/paper trades (with a
  `--stop`, the risk unit), then `python scripts/reconcile_fills.py` reads the live edge.
  Signals mature in ~25 trading days, so meaningful drift numbers are ~5 weeks out.

---

## Audit — 3-pass bughunt (2026-06-06)

Deep multi-agent review of the whole tree; every item below was verified against the code
(false positives debunked).

> **✅ ALL P1 ITEMS FIXED (2026-06-07)** — correctness + reconciliation/journaling + the named
> dead-code removals are done, each re-verified at the code and pytest-gated (suite 330 green). The
> ◻ checkboxes below are left as the historical record; see NEXT-CHAT step 2 for the consolidated
> summary. **P2 items remain open.** (No sweep/WF is running now, so engine edits are safe.)

### P1 — correctness
- ◻ **Live scanner ignores `max_open_risk` + `size_mult`** (`main.py`, NORTH STAR). The headline edge
  is produced by the 5.0 budget + regime/behavioral sizing; live alerts every fired signal,
  unsized/uncapped → live exposure ≠ the validated portfolio. Surface `size_mult` and enforce/show
  the budget (read open `positions` as consumed risk). Biggest live-vs-backtest gap.
- ◻ **Behavioral `missing_axes` key mismatch** (`behavioral/__init__.py:170-194` vs the `axis_weights`
  loop). Missing axes use short names (`breadth`/`cot`/`naaim`/`aaii`) but the loop keys on canonical
  (`breadth_state`/`positioning_state`/`sentiment_state`); only `sector_cycle` is skipped. A down feed
  is scored NEUTRAL at full weight (not excluded) and `confidence` can go negative. Score does NOT
  collapse (weight maps ARE canonical) — pollution, not collapse. Use canonical names; mark
  positioning missing only when BOTH cot+naaim absent; clamp confidence ≥0.
- ◻ **WF re-tune degradation compares baseline-IS vs tuned-OOS** (`walk_forward.py:438-453`) → understates
  overfitting; `best_is` picks max IS E[R] with no trade-count floor; OOS gets baseline+1 OFAT param.
  Report tuned-IS vs tuned-OOS (or both) + trade-count floor. **Affects how to read V5.**
- ◻ **Macro regime delta bugs** (`macro/regime.py`): inflation YoY `iloc[-12]` is an 11-mo span vs a
  12-mo comparison leg → flips STABLE/ACCELERATING (`:316`); fresh inversion scored FLAT not INVERTED
  (`:243-250`); credit percentile `len>=12` assumes monthly but HY OAS is daily (`:260`); WCS WIDE
  unreachable (Brent−WTI wrong sign, `:387`).
- ◻ **Negative-caching of failed fetches** — `info_fetcher.py:79` caches a failed market-cap `None` for
  the full staleness window; `live_price.py:66` same (5-min). Cache on success only.
- ◻ **profit-factor convention split** — `stats.compute_stats` counts r==0 as a loss; `stats_utils
  ._profit_factor` treats it neutral → same run, two PF/win-rate. Unify on r==0 neutral.

### P1 — reconciliation / journaling
- ◻ **`backtest_trades` can't reconstruct the headline** — stores per-trade `r_multiple` only; no
  `effective_r`/`size_mult`/`borrow_annual_rate` columns, so `SUM(r_multiple) ≠ backtest_runs.total_r`
  once sizing/shorts are active (latent today — headline is long-only). Add columns + populate;
  reconcilers should aggregate `effective_r`.
- ☑ **reconcile_live `max_hold_mode`** — FIXED (now uses `core.exits.max_hold_exit_due` with the
  configured mode; was hard-coded "hard"). Still owes a `_replay` parity test.
- ◻ Reconciler defaults to `MAX(id)` run (provenance-blind — can pick a scoring-ON run) and has a NULL
  bucket asymmetry (live coalesces `signal_type`→momentum, backtest GROUP BY doesn't). Tag runs / COALESCE both sides.

### P2 — dead code / dead config
- ◻ `PortfolioBacktester.run_all()` is unused (~330-line drifted dup of `run_prepped`) — delete; extract
  a shared `_maybe_time_stop` (time-stop block triplicated + a 4th copy in `main.py`).
- ◻ `--earnings-aware` flag is dead (`earnings_aware=True` hard-coded; README says "Default False").
- ◻ `_quarantine` gate dead (`filter_engine.py:552`); `:317` no-op `Path` ternary; `_run_one` re-inserts
  `sys.path` every call (`sweep.py:730`).
- ◻ `defaults.py` `settings.*` defaults are dead + disagree with code (`min_score` 50 vs 60, `hold_high`
  20 vs 15, `breadth_divergence_penalty` 0.2 vs 0.0) — route consumers through `DEFAULTS`.
- ◻ Macro `wcs_spread_state` + `policy_stance_ca` carry `risk_on_weights` but no `axis_weights` → never scored.

### P2 — look-ahead leaks (regime/behavioral feeds; modest, sizing-layer)
- ◻ COT indexed by report (Tue) not release (Fri) → ~3-day leak (`cot.py:168`).
- ◻ CPI/PCE `release_date = df.index` (period date) and unused → ~1-mo leak (`fred.py:145` + regime).
- ◻ Breadth from current S&P membership → survivorship (early-history bullish bias) — known.
- ◻ Earnings tz: `ts.date()` (exchange tz) vs `date.today()` (host tz) → ±1-day buffer boundary.

### P2 — reporting / test gaps (green ≠ correct)
- ◻ Report: profit-factor ∞ uses 3 sentinels (999 / 999.0 / 999999.9999); `recovery_days==0` renders
  "not yet recovered"; bootstrap "✓ significant" tests win_rate/PF against null=0 (wrong null).
- ◻ Tests: **no walk-forward tests at all**; the behavioral missing-axis bug passes green; `_replay`
  untested; scorer-ranked budget fill never integration-tested; no-lookahead test passes `market_dfs=None`.

### P3 — robustness / infra
- ◻ Partial-bar look-ahead if `main.py` runs intraday (mitigated by the 18:00 post-close schedule);
  held SHORT positions unhandled (only `held_long`); live shows un-slipped stop/target.
- ◻ Borrow drag not scaled by `size_mult` (shorts-only); http rate-limiter not lock-protected;
  `repair_parquet` no post-write equality check; `yf_macro` serves unbounded-stale cache as valid;
  Wilder RSI/ATR ewm-seed ≠ textbook (warmup only); `calendar.py` NFP `2026-07-02` is a Thursday.

### Debunked — verified NOT bugs
- Commission is correctly charged (per-unit-risk → portfolio-R via `× size_mult`); behavioral score does
  NOT collapse (weight maps canonical); regime `STEEPENING_FROM_INVERTED=0.0` is defensible — confirm intent only.

### Needs a call (shifts published numbers)
- ◻ `equity_curve` monthly gaps → contiguous 0-fill changes the headline 0.66 Sharpe (my Phase D harness
  already uses the contiguous convention).
- ◻ WF degradation tuned-IS-vs-tuned-OOS → larger (more honest) degradation.

### Feature — entry-gate "proof of opinion" panel on charts (committed 2026-06-06)

Replace the chart's scorer-driven "Trend Template" panel with a TRUE, direction-aware, factor-grouped
read of the actual `_signal_entry` gates + a few independent context factors from data we already
compute. Sourced from the engine so it can never drift from the real decision; decouples the chart
from the retired (non-predictive) `SignalScorer`. Engine-gated → land with the post-V5 batch.

**Design:** ~5 factor rows + a context line, graded `●●●○` marks for continuous factors, hard ✓/✗
only for true binaries. Semantics flip by direction (long ↔ short): strength (RP/RS), location
(resistance-above vs support-below "clear path"), and regime tailwind/headwind all invert.
- **TREND** — close vs MA50/MA200, MA50 slope, weekly (fold weekly here).
- **MOMENTUM** — RSI in band (show value), MACD hist sign + Δ ≥ gate×ATR, fresh cross ≤ N bars.
- **LOCATION & STRENGTH** — 52W position, RP rank, RS vs SPY (RS20/RS60), nearest VBP node
  (resistance above for longs / support below for shorts → "clear path").
- **VOLATILITY** — BB bandwidth %ile (squeeze vs expanding), `bb_z`, ATR% (early vs extended).
- **RISK** — R:R ≥ `min_rr` (show 2.50), stop as %/ATR, earnings-days (⚠ if near), gap-risk;
  **shorts add a borrow/HTB row** (`signals.borrow.*`, `hard_to_borrow_list`).
- **CONTEXT line** — regime tailwind/headwind (risk-on score, direction-aware), liquidity `dv20` $,
  open-risk budget consumed (`positions` vs `max_open_risk`), ticker-health (chronic-loser streak).

**Build:** (SHIPPED 2026-06-07 — see NEXT-CHAT step 1)
- ☑ `GateCheck(group, name, passed, detail, strength)` + `SignalResult.checks` — defined in
  `filter_engine.py` (not `types.py`: `types` already imports from `filter_engine`, so the reverse
  would cycle) and **re-exported from `core/types.py`** (`__all__`).
- ☑ `filter_engine._build_gate_checks` re-derives every factor from already-loaded data AFTER the
  decision (so replay is bit-identical *by construction*, not just by discipline), behind
  `signal(with_checks=True)`. Computes RS vs SPY (`market_dfs["SPY"]`), VBP distance (added
  `nearest_high_volume_node_below`), BB %ile/`bb_z`, ATR%, R:R, earnings, liquidity, regime.
- ☑ `core/indicators/chart.py`: new `_render_trigger_panel` (driven by `signal.checks`); the legacy
  `score_components` sidebar is kept ONLY as a fallback for exits / scoring-ON.
- ☑ `main.py`: `with_checks=True` on the live `signal()`; `_append_live_context_checks` folds in RP
  rank (LOCATION) + open-position count (CONTEXT). *Note:* `chart_signal_history.py` needed no change
  (the history overlay renders past-bar markers, not the sidebar); open-risk **budget** + **ticker-
  health** context rows are deferred to the P1 "live `max_open_risk` + `size_mult`" item (not wired live yet).
- ☑ Tests: `tests/test_trigger_panel.py` — long & short direction-aware ✓/✗ sets with values, near-miss
  row flip, replay bit-identical (all fields equal bar `checks`), VBP `_below`, Telegram `_checklist`,
  headless chart render (long+short). Visually verified the rendered panel.

---

## Validation & de-biasing

Edge after de-biasing is real-but-thin. Evidence: `docs/verification_results_2026-06.md`,
`docs/adr/ADR-001-max-hold-exit.md`. Phase A (survivorship) is closed; Phase E = the live
reconciliation in ACTIVE above.

- ◻ **Phase C — locked OOS**: tune ≤2015, lock, test 2016–2026 once.
- ◻ **Phase D — multiple-testing correction**: deflated Sharpe / White reality check.
- ◻ **V5 — full re-tune walk-forward over the whole range** (headline OOS gate, `--walk-forward
  --workers 14`; see the ACTIVE item). The fixed-config (`--wf-no-retune`) WF on the scoring-OFF
  `run_id=11` headline **PASSED** (`run_id=12`: 68% OOS, p≈0.009, −0.006 degradation). V5 is the
  slower re-tune gate that tests parameter-selection generalisation — the remaining de-bias step.
- ◻ Refresh the **deflated** Sharpe under rf=0 — still pending Phase D (White's reality check).
  (OFF baseline, `if_not_profit`, the 10–30d horizon sweep were refreshed 2026-06-05 at the new
  slippage=0.002 default — see the `ADR-001` rf=0-refresh block.)
- ◻ Behavioral sweep rows now use **real** breadth/sector (key-mismatch fixed 2026-06-05) — the
  pre-fix sweeps ran with breadth NEUTRAL-pinned, so re-run any behavioral-param tuning.

---

## Deferred — bigger work, not now

**Scoring**
- ◻ Sub-score audit: `_score_rs_entry/_exit` sanity under `direction == "short"`.
- ◻ Keep `ConfigError` guard: `scanner.weights.insider_buying`/`short_interest` stay 0 until
  Form 4 XML + live short-interest validated.

**Backtester fills** (verify in PyCharm)
- ◻ Open-EOD count regression; slippage stress across `entry_slippage_pct ∈ {0,0.002,0.003}`.

**Behavioral / macro fetchers**
- ◻ Form 4 XML parser (direct SEC EDGAR, P vs S; needs `SEC_USER_AGENT`).
- ◻ Survivorship in `sp500_constituents`/`tsx60` (date-stamped membership).
- ◻ FOMC/CPI live scrape (`calendar.py` ships a hard-coded 2026 list).
- ◻ Verify AAII/NAAIM/COT parses still match live pages (layout-drift risk).

**Reporting / observability**
- ◻ **Re-calibrate or retire the SignalScorer** (now OFF by default — ADR-003). corr(entry_score,
  R)=−0.03 (noise); turning scoring off lifted Sharpe 0.42→0.66. The scorer is retained behind
  `--scoring` for study — either make it predictive (corr>0) or delete it. Until then it's dead
  weight. Also: should the live `min_score_to_alert` gate be replaced by a smarter entry tiebreak
  (e.g. `min_rr`/ATR) rather than no ranking at all?
- ◻ Stand-down log (silent-regime months); per-direction breakdown in report.
- ☑ **Telegram — Phase 1 (push) + Phase 2 (daemon) shipped.** Phase 1 (2026-06-06): `src/core/telegram/`
  (config/format/bot/push/keyboards) + `src/core/execution/adapter.py` (broker-adapter seam, journal-only)
  + fail-open `main.py` hook + `settings.yaml telegram:` block (default OFF → scan byte-identical) +
  `python-telegram-bot` dep. Hybrid caption + chart photo; variants for entry/short/exit/watch/header/
  stand-down + position card; `format_entry(checklist=)` factor line **lit** (2026-06-07). **Phase 2
  (2026-06-08): `telegram_bot.py` interactive daemon SHIPPED** — owner-only PTB long-poll, lockfile,
  commands `/positions /pos /recalc /open /close /stop /status /chart /scan`, inline buttons, `/close`
  confirm gate (see "Done this session"). Tests: `test_telegram_format`/`_push`/`test_execution_adapter`/
  `test_trigger_panel`/`test_telegram_bot`. Plan: `docs/telegram_integration_plan.md`. **Open:** richer/
  creative templates + run the daemon live (paper-fill on-ramp).

**Watchlist expansion** (mind NORTH STAR #2)
- ☑ **Universe-agnosticism checked on v3** (2026-06-06). The 91→213 expansion showed the edge IS
  somewhat watchlist-sensitive (scoring-ON dropped E[R] +0.071→+0.046), but the composition test
  traced that to the scoring layer, and the honest broad edge with scoring OFF (+0.075/0.66) then
  passed fixed-config OOS. So the logic holds across composition once the noise gate is removed.
- ◻ Consider more `.TO` / sector ETFs and a survivorship-free constituent feed (date-stamped
  membership) so the universe itself isn't hindsight-selected.

**Architecture / performance**
- ◻ Split FilterEngine god-class / main.py / sweep.py; `ApplicationContext` DI;
  `max_concurrent_per_sector` via `sector_map.yaml`.
- ◻ `_pack_universe` → `shared_memory`; walk-forward sweep cache key incl. grid hash.
- ◻ Pin `requirements.txt` for release.

---

## Standing rules

- `pytest tests/` green at the end of every step (currently **346**).
- README sync after any landed change (CLI flags, config blocks, test counts, entry points).
  Fresh clone + `pip install -r requirements.txt` + README should run.
- **Journaling:** every run leaves data. `run_backtest.py` journals by default (`--no-journal`
  for throwaway); `main.py` auto-journals + warns if the DB is down; `reconcile_live.py` uses
  the latest backtest run (`--bt-run-id N` to override). Exploratory harnesses
  (compare / ab / frozen / walk-forward A/Bs) do NOT journal.
- **Comments document what / usage — no dev-narrative markers** ("Phase N", "Stage N", tickets).

---

## Recently shipped (condensed — full detail in commits / ADRs, branch `v3-release` / PR #2)

- **Expected-hold made data-driven + unified** (2026-06-07) — the "hold ~N–Md" caption is now
  the 25th–75th percentile of ACTUAL `backtest_trades.bars_held` (`backtest.db.expected_hold_range`,
  no upper clamp since `if_not_profit` winners run past the cap), set on every fired entry by
  `main.py` **regardless of scoring** (was scorer-only → dead under scoring-OFF). Killed the four
  disagreeing sources (`settings.market_hours` 20–30, `scoring._DEFAULT_HOLD` 10–15, `defaults.py`
  10–20, field default) → one source = the data, cap-anchored fallback. Real range from run_id=12
  (1614 trades): **(3, 15)**, avg 11.2, max 122 — the old 20–30/10–15 were both wrong. Display-only,
  zero P&L. `tests/test_hold_range.py` (+8) → suite **338 green**.
- **Fixed-config OOS validation** (2026-06-06, `run_id=12`) — `--wf-no-retune --workers 14`, 47
  windows: IS +0.066 → OOS +0.072 (degradation −0.006), **68% OOS-profitable, p≈0.009**. The
  scoring-OFF headline is temporally stable; `verification_results` updated. (Re-tune V5 + deflated
  Sharpe still pending.)
- **Scoring made opt-in, default OFF** (2026-06-05, ADR-003) — the entry score is non-predictive
  of R (corr −0.03) and its highest-score-first budget fill selected weaker trades. `--scoring`
  flag (run_backtest + main.py; `SweepEngine(use_scoring=)`), default OFF. **New headline run_id=11
  (213 universe, scoring OFF): +116.7R, Sharpe 0.66, PF 1.30, E[R] +0.075** — vs scoring-ON
  run_id=10 (+68.9R, 0.42). Chart badge no longer shows "LONG 0". `tests/test_scoring_toggle.py`
  + `tests/test_chart_no_scoring.py` (+6).
- **Watchlist v3 → 213 names** (2026-06-05) — deep Canadian bench + US large-caps, no survivorship
  pruning; the composition test (ETF 0.39 / stocks 0.56 / combined 0.42 Sharpe) surfaced the
  scoring leak above. `data/prices` all fetched/valid.
- **Live = backtest on the max-hold exit** (2026-06-05) — extracted the time-stop decision into
  `core.exits.max_hold_exit_due` (one rule, shared); refactored the 3 backtester sites to use it
  and **wired it into `main.py`** so the live scanner force-exits a held long at the cap (25d
  if_not_profit) just like the backtest. `tests/test_exits.py` (+5). Also fixed the lone pandas
  `Timestamp.utcnow` deprecation (`form4.py`).
- **Trading default → 25d `if_not_profit`** (2026-06-05) — switched the default exit from `hard`
  (validation-conservatism) to the economically-correct "let winners run" mode, which dominates
  every metric at realistic frictions: **headline +74.5R, Sharpe 0.50, PF 1.26** (vs hard
  +43.8R/0.29). Budget re-validated (`scripts/budget_sweep.py`): 5.0 still Sharpe-optimal.
  Caveat in `ADR-001` *Decision update*: extra edge leans on the unvalidated long-hold tail →
  forward-test before sizing. **This is the new headline number.**
- **rf=0 figure refresh** (2026-06-05) — re-ran OFF baseline / `if_not_profit` / 10–30d horizon
  sweep under rf=0 at the new slippage=0.002 default; `ADR-001` gains an rf=0-refresh table,
  `verification_results` updated. Headline 25d-hard now **+43.8R, Sharpe 0.29** (was +87.5R/0.58
  at slippage 0.001 — friction bump drives the drop). Deflated Sharpe still pending Phase D.
- **Phase B — realistic frictions** (2026-06-05) — `entry_slippage_pct` raised 0.001→**0.002**
  and `borrow.annual_rate_default` 0.0→**0.03** (shorts only) as conservative defaults;
  `scripts/friction_sweep.py` measures the sensitivity. Slippage bites hard: 0→+117.3R/0.72,
  0.001→+75.6R/0.48, **0.002→+43.8R/0.29**, 0.003→+14.7R/0.10 (commission mild). Edge is thin
  and slippage-sensitive — flagged in `verification_results` as the top win-now risk.
- **Behavioral key-mismatch fix + bind diagnostic** — backtest loader keyed behavioral parquet
  by stem (`sp500_breadth`/`sector_ratios`) but the classifier reads `breadth`/`sector_rotation`,
  so breadth (weight-4 axis) was NEUTRAL-pinned in every backtest/sweep; fixed via
  `loader._BEHAVIORAL_KEY_ALIASES`. `scripts/instrument_binds.py` measures bind frequency
  (fade RSI floor 8.4% — active, kept; breadth divergence 0.09% — inert, sweep row pruned).
  `tests/test_instrument_binds.py` + `tests/test_loader_behavioral_keys.py` (+12); triage 2a/2b.
- **Real-fill reconciliation** — `scripts/reconcile_fills.py` scores realized R on closed
  `positions` (initial-stop risk unit) vs `backtest_trades` by direction, flags drift; lists
  open positions as carried risk. `data/positions_schema.sql` added (fresh-clone DDL);
  `tests/test_reconcile_fills.py` (+7). README "Reconciliation" section.
- **Daily scheduling** — `scripts/register_daily_scan.ps1` + `scripts/run_daily.bat` register
  a Windows Task Scheduler job running `main.py` Mon–Fri at a local time, only-when-logged-on,
  catching up missed runs; wrapper appends to `logs/scheduler.log`. README "Schedule it daily".
- **`position_CLI.py open --date YYYY-MM-DD`** — retroactive opens (default today, ISO,
  rejects future dates) to backfill `positions`; `tests/test_position_cli.py` (+7).
- **Sharpe/Sortino** → rf=0 scale-invariant + textbook `/N` (`stats_utils`); headline `run_id=8`
  (25d-hard @ budget 5.0) = +87.2R, Sharpe 0.58, Sortino 1.03. (Superseded 2026-06-05 — that run
  predates the friction/behavioral/exit fixes; current headline is the `if_not_profit` entry above.)
- **`max_concurrent` → `max_open_risk`** aggregate-risk budget (default 5.0 = Sharpe-optimal,
  OOS-validated); `--max-open-risk` flag; `test_portfolio_risk_budget.py`.
- **breadth** full S&P 500 universe (was `[:100]`, A–C bias).
- **VBP** made canonical (H-L share-volume distribution, volume-conserving); `test_vbp.py`.
- **Magic-number fallbacks** → `DEFAULTS`; scoring shape-constants named.
- **compute_r** gap-through documented (immaterial ~0.25R); **json_cache** RMW + **dual earnings
  cache** documented (invariants hold by construction / content already unified).
- **Score-based exit** built → measured → **rejected** (`ADR-002`); exit score stays live-advisory
  (`_score_rs_exit` confirmed real, just not useful as a mechanical exit).
- **max-hold exit** (`ADR-001`, 25d-hard), Phase A survivorship, chronic-loser de-fang, report
  coloring, date-stamped screenshots, `data/backtest_schema.sql`.
- Repo hygiene: dev-narrative comment markers stripped repo-wide; `.gitignore` tightened
  (nested `__pycache__`, `logs/`).
