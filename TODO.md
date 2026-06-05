# TODO
---

> ## ★ NORTH STAR #1 (2026-06-04): WIN NOW — the backtest is secondary.
> The strategy must be **profitable in the current market**. A config that maxes the
> 2001+ backtest but is **losing today is wrong**. Evaluate/tune on **recent & current
> reality** (regime/behavioral/size-mult adaptivity, recent windows, live signals +
> open positions) — not 25-year aggregates. Deep validation (survivorship, walk-forward
> rigor) is **secondary** until live performance is healthy.
>
> ## ★ NORTH STAR #2 (2026-06-04): UNIVERSE-AGNOSTIC — don't tune to the watchlist.
> The watchlist is an input that **changes** (could be a handful or hundreds). Logic and
> parameters must hold across **any size/composition** — never overfit to the current
> ~91 names or their count. Prefer **relative/percentile/adaptive** knobs over absolute
> counts (watch `portfolio.max_concurrent`, breadth-by-fixed-count, etc.).

---

## ▶ DO / VERIFY FIRST — cleanup & patches (small, low-risk, high-leverage)

Order: things that distort the *metrics we decide on* → universe-agnostic fixes
(NORTH STAR #2) → hygiene. Run/clear these before the bigger bets.

**Metric-correctness (we quote these numbers — fix first):**
- ◻ **Sharpe/Sortino methodology** (`stats_utils.sharpe_ratio`) — TOP actionable patch.
  Hardcodes "1R ≈ 10%" while sizing is 1% fixed-risk; Sortino divides by downside-*count*
  (inflates). Every Sharpe in the ADR/validation (0.44–0.59) rides on this — correct or
  document the convention.
- ⏸ **`Trade.compute_r` 0R on gap-through-stop entry — INVESTIGATED 2026-06-04, ~non-issue.**
  Only **7 of 1098** trades gap through; the stop fills at the same open so
  `exit ≈ entry − slippage` *always* → realized loss is just the entry slippage (~−0.03R
  each, ~**−0.25R** total on a +132R ledger). The TODO's "understates the left tail"
  premise was overstated — these are cost-only scratches, not hidden losers.
  **Recommended: document** it in `compute_r` (gap-through = scored ≈0 by design) and
  close; the `intended_risk` plumbing (4 files) isn't worth 0.25R. *Decision pending*
  (user picked "record true loss", but the measured impact is immaterial — re-confirm).

**Universe-agnostic (NORTH STAR #2):**
- ◻ **`compute_sp500_breadth` truncates `constituents[:100]`** (`breadth.py:59`) →
  A-C alphabetical bias + assumes a fixed count. Thread the full universe (or make it
  a true sample). Direct NORTH STAR #2 violation.
- ◻ **`portfolio.max_concurrent: 6`** is a fixed cap tuned to ~91 names — wrong for a
  tiny list, a bottleneck for a huge one. Make it relative (e.g. % of universe / risk
  budget) or document the assumption.

**Hygiene / reproducibility:**
- ✅ **`data/backtest_schema.sql` — CREATED 2026-06-04** (was missing; elevated now
  that journaling is default-on so fresh deploys can journal).
- ◻ **Inline magic-number fallbacks → `defaults.py`**: `gap_risk` 3.0
  (`filter_engine.py:535`), `max_bars_since_cross` 3 (`:933/982`), dv20 window 20,
  `_score_rs_exit` ×10, `_score_bb_zscore` /2.0 (`scoring`). They read config but the
  fallback default is inline — centralise.
- ◻ **`json_cache.save_section` RMW not lock-safe** — safe today (single writer); add a
  file lock or document the single-writer assumption.
- ◻ **Dual earnings cache** — `earnings_history.py` (`data/fundamentals/`) vs
  `earnings_history_store.py` (`data/earnings_history/`); two staleness clocks can
  drift. Consolidate.
- ◻ **VBP not canonical** — `compute_vbp` bins `close × volume` in the close bin, not
  the H-L spread (`scanner.vbp.*` is wired, the algorithm isn't). Rewrite or rename.

**Verified DONE this session (do NOT re-do):** ✅ `DEFAULT_SCALE` + `run_backtest`
print-fallback synced to `{2:0.5,3:0.25}` (`ticker_health.py:59`); ✅ sweep dead-key
`size_mult_floor`; ✅ report-coloring convention; ✅ max-hold `time_stop` exit.

---

## ★ ACTIVE — win now (NORTH STAR #1)

- ◻ **Run `main.py` DAILY (schedule it).** The live-reconciliation feed is the only
  way to judge "winning now", and it's just ~1 week / 62 signals old. Windows Task
  Scheduler → `main.py` after the US close; signals mature in ~25 trading days.
- ◻ **Live-vs-backtest reconciliation — BUILT, but limited.** `scripts/reconcile_live.py`
  + `scan_results` enrichment (`stop_price`/`target_price`/`signal_type` via
  `data/scan_results_recon_migration.sql` + `db.py`). **Honest caveat (2026-06-04):**
  replaying live-fired signals through cached prices ≈ a *delayed backtest* — it only
  checks signal-generation fidelity. The version that isn't a backtest reconciles
  **actual fills** (`positions`, currently empty) vs the model. **Next:** when real /
  paper trades are logged via `position_CLI`, repoint reconcile at closed `positions`
  (realized fills vs expectancy) — that's the true win-now meter.
- ✅ **Max-hold exit — headline = 25-bar hard** (`ADR-001`). Real but thin edge
  (deflated Sharpe ~0.44; survivorship discount ~11–22%, honest universe still
  positive). Optional/secondary: V5 walk-forward+robustness OOS gate; set
  `execution.max_hold_days: 25` as default after live performance is confirmed.

---

## Raw-notes triage (2026-06-03) — mostly resolved

Detail + file:line evidence: `docs/triage_raw_notes_2026-06.md`.
- ✅ Note 1 — max-hold exit / artificial WR → fixed (see ACTIVE above, `ADR-001`).
- ✅ Note 2 — sweep dead-key fixed; breadth-divergence penalty is *wired-but-dormant*.
- ✅ Note 3 — report coloring convention applied.
- ✅ Note 4 — consecutive-loss guard already exists (`TickerHealth`); scale de-fanged
  to `{2:0.5,3:0.25}`; A/B shows a tiny net-positive variance effect.
- ◻ (secondary) Instrument how often the momentum-fade RSI floor / breadth-divergence
  flag actually *bind* — to decide prune/keep. Cheap, not urgent.

---

## Secondary / paused — validation & de-biasing (per NORTH STAR #1)

Design: `docs/validation_program_design.md` (note: file currently deleted in tree —
restore or drop the reference). Edge after de-biasing is real-but-thin; deep rigor
is paused until live performance is healthy.
- ✅ **Phase A — survivorship** (A0 symbology; A1 frozen-universe A/B run 2026-06-04 →
  selection discount ~11–22%, honest universe stays positive). Gate closed.
- ◻ **Phase B — realistic frictions**: costs/slippage/borrow ON by default + sweep
  (slippage bites hard: 0→+117.5R, 0.002→+65.7R).
- ◻ **Phase C — locked OOS**: tune ≤2015, lock, test 2016-2026 once.
- ◻ **Phase D — multiple-testing correction**: deflated Sharpe / White reality check.
- ◻ **Phase E — live reconciliation**: the ACTIVE item above (paper-trade + reconcile).
- ◻ V5 walk-forward + robustness on 25d-hard (OOS gate for the headline).

---

## Deferred — bigger work, not now

**Scoring** (`scoring.py`, `defaults.py`, `settings.yaml`)
- ◻ Sub-score audit: `_score_rs_entry/_exit` sanity under `direction == "short"`.
- ◻ Keep `ConfigError` guard: `scanner.weights.insider_buying`/`short_interest` stay 0
  until Form 4 XML + live short-interest validated.

**Backtester fills** (verify in PyCharm)
- ◻ Open-EOD count regression; slippage stress test across `entry_slippage_pct ∈
  {0,0.002,0.003}`.

**Behavioral / macro fetchers**
- ◻ Form 4 XML parser (direct SEC EDGAR, P vs S; needs `SEC_USER_AGENT`).
- ◻ Survivorship in `sp500_constituents`/`tsx60` (date-stamped membership = Phase A
  for tier_b).
- ◻ FOMC/CPI live scrape (calendar.py ships a hard-coded 2026 list).
- ◻ Verify AAII/NAAIM/COT parses still match live pages (layout-drift risk).

**Reporting / observability**
- ✅ **Signal screenshots date-stamped 2026-06-04** — `data/screenshots/{TICKER}_{Dmonyy}.webp`
  using the signal bar's date (e.g. `URA_4jun26.webp`); daily shots no longer overwrite
  (`chart.py`).
- ◻ Stand-down log (silent-regime months block); per-direction breakdown in report.
- ◻ Telegram alerts (`TG_CHAT_ID`/`TG_BOT_TOKEN` reserved, unwired).

**Watchlist expansion** (mind NORTH STAR #2 — don't tune to it)
- ◻ ~15 more `.TO` ETFs into tier_a; ≤20% individual stocks; >5y history.

**Architecture / performance** (defer)
- ◻ Split FilterEngine god-class / main.py / sweep.py; `ApplicationContext` DI;
  `max_concurrent_per_sector` via `sector_map.yaml`.
- ◻ `_pack_universe` → `shared_memory`; walk-forward sweep cache key incl. grid hash.

**Operational**
- ◻ `position_CLI.py open --date YYYY-MM-DD` (retroactive opens — needed to backfill
  `positions` for the real reconciliation). Pin `requirements.txt` for release.

---

## Standing rules

- `pytest tests/` green at the end of every step (192 + max-hold/ticker = **197**).
- README sync after any landed change (CLI flags, config blocks, test counts, entry
  points). Fresh clone + `pip install -r requirements.txt` + README should run.
- **JOURNALING POLICY (2026-06-04): every run leaves data.** `run_backtest.py` journals
  by default (`--no-journal` for throwaway); `main.py` auto-journals + warns loudly if
  the DB is down. `reconcile_live.py` uses the latest backtest run as reference and
  prints which one (`--bt-run-id N` to override). Exploratory harnesses
  (compare/ab/frozen) do NOT journal.

---
