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

## ★ ACTIVE — win now (NORTH STAR #1)

The metric / universe-agnostic / hygiene cleanup tier is cleared (see *Recently shipped*).
These are the live-performance items that actually move NORTH STAR #1.

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

## Validation & de-biasing (paused per NORTH STAR #1 until live is healthy)

Edge after de-biasing is real-but-thin. Evidence: `docs/verification_results_2026-06.md`,
`docs/adr/ADR-001-max-hold-exit.md`. Phase A (survivorship) is closed; Phase E = the live
reconciliation in ACTIVE above.

- ◻ **Phase C — locked OOS**: tune ≤2015, lock, test 2016–2026 once.
- ◻ **Phase D — multiple-testing correction**: deflated Sharpe / White reality check.
- ◻ **V5 — walk-forward + robustness on 25d-hard** (headline OOS gate). NB: the 5.0 open-risk
  budget already passed a fixed-config walk-forward (2026-06-05); V5 is the full re-tune
  robustness gate for the *headline config*.
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
- ◻ Telegram alerts (`TG_CHAT_ID`/`TG_BOT_TOKEN` reserved, unwired).

**Watchlist expansion** (mind NORTH STAR #2)
- ◻ **Re-run the headline on the v3 universe** (213 names) to confirm universe-agnosticism —
  if per-trade E[R]/Sharpe hold (~+0.07 / ~0.50) the edge isn't watchlist-specific; if they
  collapse, the old 91-name edge was survivorship. (v3 grew tier_a 91→213, deep Canadian bench
  + US large-caps, all >5y history; individual-stock share now ~50% — a deliberate departure
  from the old ETF-heavy ≤20% rule, accepted for more momentum vehicles.)

**Architecture / performance**
- ◻ Split FilterEngine god-class / main.py / sweep.py; `ApplicationContext` DI;
  `max_concurrent_per_sector` via `sector_map.yaml`.
- ◻ `_pack_universe` → `shared_memory`; walk-forward sweep cache key incl. grid hash.
- ◻ Pin `requirements.txt` for release.

---

## Standing rules

- `pytest tests/` green at the end of every step (currently **244**).
- README sync after any landed change (CLI flags, config blocks, test counts, entry points).
  Fresh clone + `pip install -r requirements.txt` + README should run.
- **Journaling:** every run leaves data. `run_backtest.py` journals by default (`--no-journal`
  for throwaway); `main.py` auto-journals + warns if the DB is down; `reconcile_live.py` uses
  the latest backtest run (`--bt-run-id N` to override). Exploratory harnesses
  (compare / ab / frozen / walk-forward A/Bs) do NOT journal.
- **Comments document what / usage — no dev-narrative markers** ("Phase N", "Stage N", tickets).

---

## Recently shipped (condensed — full detail in commits / ADRs, branch `v3-release` / PR #1)

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
