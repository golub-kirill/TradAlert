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
> must hold across **any size/composition** — never overfit to the current ~91 names or
> their count. Prefer **relative/percentile/adaptive** knobs over absolute counts. (Known
> offenders fixed: `max_concurrent`→`max_open_risk` budget, breadth now full-universe —
> keep the principle in mind for any new knob.)

---

## ★ ACTIVE — win now (NORTH STAR #1)

The metric / universe-agnostic / hygiene cleanup tier is cleared (see *Recently shipped*).
These are the live-performance items that actually move NORTH STAR #1.

- ◻ **Run `main.py` DAILY (schedule it).** The live feed is the only way to judge "winning
  now" and it's only ~1 week old. Windows Task Scheduler → `main.py` after the US close;
  signals mature in ~25 trading days.
- ◻ **Live-vs-backtest reconciliation on REAL fills.** `scripts/reconcile_live.py` exists, but
  replaying live signals through cached prices ≈ a *delayed backtest* (signal-fidelity only).
  The real meter reconciles **actual fills** (`positions`, currently empty) vs the model —
  blocked on logging real/paper trades (see `position_CLI` below).
- ◻ **`position_CLI.py open --date YYYY-MM-DD`** (retroactive opens) — needed to backfill
  `positions` so the real reconciliation above has data.

---

## Validation & de-biasing (paused per NORTH STAR #1 until live is healthy)

Edge after de-biasing is real-but-thin. Evidence: `docs/verification_results_2026-06.md`,
`docs/adr/ADR-001-max-hold-exit.md`. Phase A (survivorship) is closed; Phase E = the live
reconciliation in ACTIVE above.

- ◻ **Phase B — realistic frictions**: costs/slippage/borrow ON by default + sweep
  (slippage bites hard: 0→+117.5R, 0.002→+65.7R).
- ◻ **Phase C — locked OOS**: tune ≤2015, lock, test 2016–2026 once.
- ◻ **Phase D — multiple-testing correction**: deflated Sharpe / White reality check.
- ◻ **V5 — walk-forward + robustness on 25d-hard** (headline OOS gate). NB: the 5.0 open-risk
  budget already passed a fixed-config walk-forward (2026-06-05); V5 is the full re-tune
  robustness gate for the *headline config*.
- ◻ Refresh the remaining Sharpe/Sortino figures (OFF baseline, `if_not_profit`, horizon sweep,
  deflated) under the rf=0 convention — they predate the fix (relative ranks unaffected).

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
- ◻ Stand-down log (silent-regime months); per-direction breakdown in report.
- ◻ Telegram alerts (`TG_CHAT_ID`/`TG_BOT_TOKEN` reserved, unwired).
- ◻ (cheap) Instrument how often the momentum-fade RSI floor / breadth-divergence flag
  actually *bind* — to decide prune/keep.

**Watchlist expansion** (mind NORTH STAR #2)
- ◻ ~15 more `.TO` ETFs into tier_a; ≤20% individual stocks; >5y history.

**Architecture / performance**
- ◻ Split FilterEngine god-class / main.py / sweep.py; `ApplicationContext` DI;
  `max_concurrent_per_sector` via `sector_map.yaml`.
- ◻ `_pack_universe` → `shared_memory`; walk-forward sweep cache key incl. grid hash.
- ◻ Pin `requirements.txt` for release.

---

## Standing rules

- `pytest tests/` green at the end of every step (currently **207**).
- README sync after any landed change (CLI flags, config blocks, test counts, entry points).
  Fresh clone + `pip install -r requirements.txt` + README should run.
- **Journaling:** every run leaves data. `run_backtest.py` journals by default (`--no-journal`
  for throwaway); `main.py` auto-journals + warns if the DB is down; `reconcile_live.py` uses
  the latest backtest run (`--bt-run-id N` to override). Exploratory harnesses
  (compare / ab / frozen / walk-forward A/Bs) do NOT journal.
- **Comments document what / usage — no dev-narrative markers** ("Phase N", "Stage N", tickets).

---

## Recently shipped (condensed — full detail in commits / ADRs, branch `v3-release` / PR #1)

- **Sharpe/Sortino** → rf=0 scale-invariant + textbook `/N` (`stats_utils`); headline `run_id=8`
  (25d-hard @ budget 5.0) = +87.2R, Sharpe 0.58, Sortino 1.03.
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
