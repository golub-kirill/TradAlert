# TODO

Project backlog. Read **State of play** first, then **Active work**, then
pick from the named sections below.

Phases used to be numbered (`Phase 1`, `Phase 6`, etc.) — that proved
confusing once we hit multi-part work. Going forward we name each block
by what it touches (e.g. "Scoring system", "Backtester fills"). The
phase numbers stay only as historical references in commit messages.

---

## State of play (as of 2026-05-30)

- All production .py files parse and import; baseline backtest is
  stable at **111 trades · WR 50.5% · +33.07R · max DD 9.80R** over
  2023-01-01 → 2026-05-27. (Re-confirm the regression run in PyCharm
  after this session's changes — sandbox has no `yfinance`.)
- **Fetch-error hardening (2026-05-30/31) — VERIFIED against fresh log
  `tradealert-abf60697.log`:**
  - *BoC* — ✅ **CONFIRMED FIXED.** Latest runs show `[macro] 22/22 series
    fetched, 0 failed` (was 19/22 with the V39055/56/57 404s). No BoC
    tracebacks in post-fix runs.
  - *Fail-open-quiet* — ✅ confirmed: post-fix the fetch failures log as
    single WARNING lines, no stack traces (the 24 tracebacks in the log are
    all from pre-fix runs on 05-29 / 05-30-08:04).
  - *NAAIM* — ✅ working: `[naaim] updated cache (1 rows)` via the live scrape.
  - *AAII* — ⚠ still unavailable: AAII now 403s even the homepage
    (`https://www.aaii.com/`), so the cookie-priming approach is also blocked.
    Fails open cleanly (no traceback). Behavioral confidence rose 25%→50%
    (NAAIM recovered). Needs a different source (e.g. a third-party mirror or
    manual cache seed).
  - *COT* — ⚠ still 404 + deeper bug: both `72hh-3qaq` and `s9da-n2w9` 404.
    Now pointed at `72hh-3qpy` (UNVERIFIED — sandbox couldn't reach CFTC).
    **Real fix needed:** ES/UST/VIX are *financial* futures → they live in the
    **TFF** report, not Disaggregated, and use `lev_money_*` columns, so
    `cot.py` needs both the right TFF resource ID and a column-mapping change
    in `_normalise_cot_rows`. Fails open meanwhile.
  - **Original details below.**
  - *Divide-by-zero (`div_pct = div / df2['Close']…`)* — investigated; the
    offending `history.py` / `div_pct` code **no longer exists** anywhere in
    the tree (already removed in a prior refactor). All live scalar divisions
    in `scoring.py` / `rp_rank.py` / `regime.py` are already guarded
    (`if x > 0 …` / try-except). No code change needed — do NOT reintroduce.
  - *BoC 404 (V39055/56/57)* — **fixed**: the V-series IDs were retired;
    swapped to the current `BD.CDN.{2,5,10}YR.DQ.YLD` benchmark IDs in
    `config/settings.yaml` (live-verified: HTTP 200, parser-compatible nested
    shape). These three series are fetched/cached but consumed nowhere (only
    `V39079` overnight rate is read by `regime.py`), so the swap restores the
    feed with **zero impact on any calculation**. `boc.py` now catches
    `requests.HTTPError` separately and logs one clean line on 404 (no more
    traceback spam); generic failures also fail-open-quiet to cache/empty.
  - *AAII 403* — `aaii.py` reworked: primes cookies on the homepage, then
    scrapes the `sent_results` HTML table (`_parse_aaii_html`); fails open to
    cache/empty. (xls download is permanently 403 for bots.)
  - *NAAIM 404* — `naaim.py` reworked: loads cached history + scrapes the
    latest value from `naaim.org/resources/naaim-exposure-index/`; fails open.
  - *CFTC COT 404* — `cot.py` resource ID changed `72hh-3qaq` → `s9da-n2w9`;
    fail-open-quiet on 404/rate-limit. **VERIFY** the new ID points at the
    intended Disaggregated dataset before trusting COT (a wrong Socrata ID
    returns a different dataset silently). Research suggested `72hh-3qpy`
    (Disaggregated Futures-Only) — reconcile in PyCharm.
  - FRED/yfinance: left as-is this pass (FRED already masks the api_key and
    fails open per series; yfinance 401/timezone are upstream/library issues).
  - **Note:** parts of this work were done concurrently by another agent
    editing the same files; the above reflects the verified on-disk state.
- **Short Trading is code-complete** (Phases 10.1–10.6 + v2 polish):
  scoring direction-flip, `--allow-shorts` CLI + SHORTS/COVERS summary,
  the `validate_shorts` harness, concurrency guard, and v2 (min_rr_short,
  hard-to-borrow list, borrow-cost R drag, signal_type-aware MR flip).
  All short config defaults OFF → long-only baseline replays identical.
  `--allow-shorts` is wired into **both** `main.py` and
  `backtest/run_backtest.py`. **10.6 validated 2026-05-30: shorts are NOT
  additive** over 2001-2026 (+99 shorts ≈ −1.6R, WR 32%, Sharpe 0.62→0.57,
  recovery 1143→1981d) — **keep `signals.allow_shorts: false` in
  production**; the plumbing is correct but the short *selection* has no
  edge yet (see `postmortem_2026-05-30.md` § Short validation).
- **`--journal` fixed (2026-05-30):** `run_backtest.py` never called
  `load_dotenv()`, so `DB_*` from `secrets.env` never reached the
  backtest process → journaling silently no-op'd. It now loads
  `config/secrets.env` at startup (like `main.py`). The
  `backtest_runs`/`backtest_trades` tables (`data/backtest_schema.sql`)
  must exist before `--journal` will write.
- **Full-history run + postmortem (2026-05-30):**
  `data/backtest_out/postmortem_2026-05-30.md`. Long-only 2001→2026:
  **1,157 trades · WR 47.0% · +142.5R · PF 1.38 · max DD 30.98R**.
  Key takeaways: stops are clean (mean −1.00R, tiny tail); 74% of trades
  close via the engine-exit (highest-leverage knob); **zero BEAR/CHOP
  trades** (long-only gate sits flat ~25y of non-bull) — the strongest
  case for shorts; chronic losers = energy/commodity/dollar/value ETFs.
- Unit-test count: **163 passing** (3 deselected — live-network ones).
  `pytest tests/` from the repo root. New 2026-05-28: +9
  `test_short_scoring.py` (10.4 flip), +2 `test_short_e2e.py` (10.5
  smoke), +1 `test_short_portfolio_guard.py` (10.6 concurrency), +7
  `test_short_v2.py` (min_rr_short + HTB list), +7 `test_short_borrow.py`
  (borrow-cost R drag), +3 `test_short_mr_flip.py` (MR-short flip), +2
  `test_no_lookahead.py` (no-look-ahead + open-EOD invariants).
- Opt-in flags/config; each defaults OFF/neutral so the baseline
  replays bit-identical:
  - `--chronic-penalty` (per-ticker sliding-scale size penalty)
  - `--vix-slope-gate` (block momentum when VIX rising)
  - `--anti-gap-entry` (require trigger bar close ≥ open)
  - `signals.allow_shorts: false` (short-side master switch) +
    `main.py --allow-shorts`
  - `signals.require_trigger_bar_up: false` (same as `--anti-gap-entry`)
  - `signals.stop_loss.min_rr_short` (asymmetric R:R for shorts; absent
    → falls back to `min_rr`)
  - `signals.hard_to_borrow_list: []` (block shorts on listed symbols)
  - `signals.borrow.{annual_rate_default: 0.0, per_ticker}` (short
    borrow-cost R drag)
- Behavioral / macro fetchers reimplemented fresh (2026-05-28); they
  read live or fail-open to neutral data. Live fetches need real
  network — they don't run in the sandbox.
- Reports show: stop-out latency histogram, monthly P&L with zero-trade
  months backfilled, per-ticker attribution tie-broken by worst trade,
  Position Sizing & Kelly section now leads with 1% fixed risk.
- Watchlist trimmed from 100 → 91 tier_a: removed MTUM, SIZE, SPHB,
  EWG, EWU, USO, LIT, SU.TO, SHOP.TO per postmortem chronic-loser
  analysis. Baseline shifted from +24R / -11.7R DD to +33R / -9.8R DD.

---

## Implemented from the audit (2026-05-31)

Done and verified (suite **166 pass from repo root**; full import chain OK):

- **Test infra**: root `conftest.py` (pytest works from repo root), `yfinance`
  import made lazy in `earnings_history.py`, `tests/` un-gitignored.
- **paths.py adopted** across all fetchers + caches (cache, json_cache,
  live_price, earnings_history_store, behavioral/*, constituents, ticker_store,
  chart, macro/*, fetcher) + added `PRICES_LIVE_DIR`. Paths now project-root
  anchored (CWD-independent).
- **filelock removed** from requirements.txt (never imported).
- **db_conn consolidated 3/3**: `db.py` and `position_manager.py` delegate to
  `db_conn.connect()`; `connect_timeout=5` moved into the canonical helper;
  dead imports/`_DB_OPTIONAL_KEYS` removed.
- **vix_high/vix_low defaults** now read `core.defaults.DEFAULTS` in
  `filter_engine` (was inline `25` ≠ config `28`).
- **scanner.vbp.{lookback,n_bins,volume_percentile}** wired into scoring (was
  hardcoded 120/24/70; added to `ExitThresholds`).
- **mask_api_keys_filter** installed on both log handlers in `main`.
- **types.py**: `TickerResult` migrated here from `main.py`; `db.py` imports
  from `core.types` — removes the persistence→application inversion.
- **Dead code**: removed unused `MacroState` import and `exclude` local.

Also implemented (user-approved 2026-05-31):

- **Reporting now uses `effective_r`** (size_mult + borrow drag) — equity
  curve, stats, attribution, report, run_backtest. ⚠ This changes the headline
  R numbers (trade *count* unchanged): the macro/behavioral size multiplier now
  scales each trade's contribution. Re-run the baseline in PyCharm to capture
  the new headline. Tests: `test_audit_fixes.py`.
- **`signals.size_mult_gate` implemented** in `_signal_entry` (blocks entries
  when composite size_mult < min). Defaults OFF → baseline replays identically
  until enabled. Added `DEFAULTS["filters.signals.size_mult_gate.min"]`.
- **O(n²) hot loop fixed**: `_ticker_trend` reads the precomputed `ma_fast`/
  `ma_slow` columns (fallback to O(period) tail-mean); `_classify_trend`,
  `_market_regime`, `_sector_strength_ok`, scoring MA50/200 + slopes, and
  `_regime_detail` no longer recompute full-series rolling per bar. Proven
  result-identical (`test_ticker_trend_column_path_matches_rolling_recompute`,
  140+ bars × random series, 0 mismatches); ~3x on the ticker-trend path.
  The previously-dead precomputed MA columns are now actually consumed.

---

## Implemented batch 3 (2026-05-31)

- **`db.signal_kind` now maps shorts** (`entry_short`/`exit_short`) — was
  long/exit_long only, dropping short rows to `"none"`. ⚠ If the live
  `scan_results.signal_kind` column is a constrained ENUM, add the two values
  via `ALTER TABLE` (insert fails fail-open otherwise).
- **Symbology applied at the equity-ticker yfinance sites**: `info_fetcher`,
  `live_price`, `earnings_history`, `behavioral/form4`, `behavioral/short_interest`
  now call `yf.Ticker(to_yf_symbol(ticker))` (was 1/8 — only `yf_fetchOne`).
  `macro/yf_macro` deliberately excluded (already Yahoo-form). Baseline-safe:
  `to_yf_symbol` is identity for clean watchlist tickers (SPY, RY.TO, ^VIX).
- **Bug-hiding log levels fixed**: backtester `_call_engine` (both sites) now
  splits `InsufficientDataError` (expected → DEBUG) from unexpected engine
  errors (→ WARNING); `sweep` no longer silently `pass`es a failed settings
  mutation (now logs WARNING with the param). Surfaces real failures that were
  invisible at the default log level.

---

## Implemented batch 4 (2026-05-31)

- **`scan()` NaN guard**: a warmup last bar (NaN close/atr) now returns a
  failed `ScanResult` instead of silently passing the volatility gate (NaN
  comparisons evaluate False). Live-only — `scan()` is called only from
  `main`, never the backtester/tests, so zero baseline/test impact.
- **min-rows unified to `trend.ma_slow`**: `scan()` and `main._run_pipeline`
  now guard at `ma_slow` (was 20 / 20), matching `signal()`'s `_min_rows_guard`.
  Tickers with <200 bars are filtered consistently at one threshold instead of
  passing scan then failing signal. Tests: `test_audit_fixes.py`
  (`test_scan_requires_ma_slow_rows`, `test_scan_blocks_nan_indicators_...`).

---

## Implemented batch 5 (2026-05-31)

- **`ConfigError` weight guard implemented**: `SignalScorer.__init__` now raises
  `ConfigError` when `scanner.weights.insider_buying` or `short_interest` > 0
  (the backing fetchers are placeholders). Makes the long-standing README/TODO
  claim true. Baseline-neutral (those weights are absent/0 in shipped config).
  Tests in `test_audit_fixes.py`.
- **README `SEC_USER_AGENT`** row corrected to "reserved / not wired" (form4
  uses yfinance, not direct EDGAR; the var is for the planned Form 4 XML parser).

All seven Trust-discipline "phantom consolidation" items in `AGENTS.md` §4 are
now closed.

---

## Implemented batch 6 (2026-06-02)

- **Core-math unit tests added** (`tests/test_core_math.py`, 15 tests) — closes
  the Phase 7 🔴 "headline-number math is untested" gap with exact-value /
  independently-recomputed assertions:
  - indicators: RSI edge cases (flat→50, rally→100, warmup→NaN), ATR
    constant-TR convergence, MACD structural identity, Bollinger = SMA±2σ + z.
  - stats_utils: Kelly known values + zero-floor, drawdown/max_drawdown exact,
    profit_factor edges (mixed/all-loss/no-loss/empty), Sharpe nan-on-zero-var,
    Sortino inf-on-no-negatives.
  - validator: clean-frame pass + dtype cast, NaN-row drop, close≤0 hard-raise
    (via an internally-consistent non-positive bar — the drop-vs-raise ordering
    is now documented in the test), missing-column raise.
    Suite now **190 pass** (was 166 baseline). Baseline-neutral (tests only).

---

## Implemented batch 7 (2026-06-02)

- **CacheBackend dedup**: new `src/core/fetchers/cache_meta.py` with the two
  byte-identical primitives `is_fresh(path, max_age_seconds)` and
  `write_meta(meta_path)`. Migrated 7 fetchers — `macro/{fred,boc,yf_macro}`,
  `behavioral/{naaim,cot,form4,short_interest}` — removing **12 duplicated
  local defs** (7×`_cache_fresh` + 5×`_write_meta`). Behavior-neutral:
  `(now−mtime)/3600 < hours` ⟺ `(now−mtime) < hours×3600` (and the days form),
  meta content unchanged. The per-fetcher `_load_cached_or_empty`/`_load_cache`
  loaders were **left local** (return shapes genuinely differ: macro=value-col
  empty DF, cot=bare empty DF, naaim=exposure-checked, form4/short_interest=
  dict). Removed the now-unused `datetime`/`json` imports. pyflakes-clean;
  import smoke + 190-test suite green. (`aaii`/`breadth`/`live_price`/
  `earnings_history_store` have freshness entangled in load fns — left as-is.)

---

## Implemented batch 8 (2026-06-02)

- **behavioral sentiment z-score → trailing 52-week window** (`_classify_
  sentiment`). Was full-series mean/std (anchored to all history); now uses
  `spread.tail(52)` so the EUPHORIA/PANIC/FEAR classification reflects the
  *current* sentiment regime. ⚠ **NOT baseline-neutral** — this feeds the
  behavioral `size_mult`, which flows into `effective_r`/reported R. The
  +111.6R baseline will shift; **re-run the backtest baseline** to capture the
  new headline. Test: `test_sentiment_zscore_uses_trailing_52w_not_full_series`.
  Suite **191 pass**.

---

## Audit cross-check (2026-05-31) — gaps this TODO does not yet track

External code audit (Phases 1-2: project map + data pipeline). Items
below are **verified against current on-disk code**, not memory. They are
NOT in the existing backlog sections and should be folded in.

- 🔴 **Recurring "phantom consolidation" pattern** — five refactors are
  declared complete in docstrings/requirements but are partial or absent
  in code:
  - `src/core/paths.py` — docstring "All callers import from here"; **0
    imports anywhere**. Orphan module (11 unused path constants).
  - `src/core/types.py` — docstring says `TickerResult` moved here, but
    the live `TickerResult` still lives in `main.py:89` and
    `persistence/db.py:30` still imports it `from main`. `types.py` copy
    is dead; only `DIRECTION`/`sign_of` are used (by one test).
  - `persistence/db_conn.py:11` — "All three call sites import from here";
    only `backtest/db.py:174` does. `persistence/db.py:236` and
    `core/position_manager.py:270` still keep their own `_connect`.
  - `filelock` (requirements.txt, "Hard-required… else json_cache loses
    sections") — **not imported anywhere**; `json_cache.save_section`
    RMW has neither filelock nor a threading lock.
  - `http.py:185` `mask_api_keys_filter` — docstring "Install at root
    logger in setup_logging"; **`addFilter` is never called**. The
    API-key log-masking defense is dead (FRED key protected only by
    fred.py's manual `_safe_url`/`type(exc)` discipline).
- 🔴 **A0 symbology is 1/8 wired, not "done"** — see annotation on the A0
  bullet in the Validation program below.
- 🟠 **`scan()` vs `signal()` min-rows mismatch** — `filter_engine.py:368`
  guards `< 20`; `_min_rows_guard` (`:1146`) guards `< ma_slow` (200);
  `main._MIN_ROWS=20`. Tickers with 20-199 bars pass scan, then always
  fail signal with `InsufficientDataError`. `scan()` also has no NaN guard
  on indicator columns (NaN comparisons silently pass the volatility gate).
- 🟠 **Dead precomputed columns** — `indicators.attach_indicators` writes
  `ma_fast`/`ma_slow`/`weekly_sma10` (docstring: "hot path reads
  `row['ma_fast']` in O(1)"), but engine/scoring/backtesters all
  **recompute** via `close.rolling(...)`. Columns read nowhere; the
  claimed optimization is unwired.
- 🟠 **`db.py:209` signal_kind drops shorts** — maps only `long`/`exit_long`;
  with `--allow-shorts`, short signals persist as `"none"` in scan_results.
- 🟠 **Cache-helper duplication ×7+** — `_cache_fresh`/`_write_meta`/
  `_load_cached_or_empty` copy-pasted across fred/boc/yf_macro/naaim/aaii/
  cot/form4/short_interest + constituents. ~5 incompatible cache
  conventions, no shared backend.
- 🟠 **Rate-limiter race** — `http.py:43-50` releases the lock before
  `sleep`+update of `_last`; concurrent callers are not serialized
  (matters for SEC/CFTC limits).
- 🟡 **`cot.py` internal staleness** — module docstring (`:22`) still names
  the dead `72hh-3qaq`; code (`:62`) uses `72hh-3qpy`. Fix docstring when
  the resource ID is finally verified.
- 🟡 **"Unused local symbols — remaining 8" (Placeholder section)** — fresh
  sweep done. Confirmed assigned-never-read: `fetcher.py:227 exclude`,
  `equity_curve.py:245 avg_bars`, `portfolio_backtester.py:112
  tickers_walked`, `stats_utils.py:49 n_samples`, `sweep.py:343 n_tickers`,
  `filter_engine.py:205 timeframe`, `trade.py:66 entry_score_components`,
  `scoring.py:221 timeframe`. Also `f"..."`-without-placeholder at
  `report.py:160,265,448` and `run_backtest.py:169,284`.

### Phase 4 — math audit (2026-05-31)

Indicators (ATR/RSI/MACD/BB), fill model (gap-aware, symmetric long/short),
bar ordering (same-bar entry+stop captured, same-bar stop+target pessimistic),
Kelly, bootstrap, MC-drawdown, profit-factor edge handling, regime/behavioral
composites, additive-R (no compounding) — **all verified correct**. Issues:

- 🔴 **`size_mult` / borrow drag do NOT reach any reported metric.** Only
  `effective_r` includes them, and `effective_r` is used solely by
  `PortfolioBacktester` `dd_gate.record()` (4 sites). Equity curve
  (`equity_curve.py:155`), `stats.py:77`, `report.py:702,712`,
  `run_backtest.py:226,345`, `backtest/db.py:161` all sum raw `t.r_multiple`.
  So the entire macro (`regime.py` 452 LOC) + behavioral (405 LOC) sizing
  system is **invisible to headline R/Sharpe/DD** — it only gates *which*
  trades fire (`size_mult_gate`) and feeds an internal DD counter. README:282
  ("backtester scales R-distance by it") is therefore **wrong**. Decide:
  per-unit-risk reporting by design (then docs + dead-complexity note), or
  bug (equity curve should use `effective_r`).
- 🟠 **`Trade.compute_r` returns 0.0 for entry-gap-through-stop**, contradicting
  the file docstring (`trade.py:6-8`: "still produces a meaningful (negative)
  R rather than being discarded"). When T+1 open fills at/through the stop,
  `risk_per_share <= 0` → `compute_r` returns 0.0 (`:157-158`). These trades
  are then counted as losses at 0R (`stats.py:79`), which **inflates
  `avg_loser_r` toward zero** and understates the left tail. Narrow (entry
  gap only; exit-gap is modeled correctly as r<−1) but optimistic.
- 🟠 **Scoring neutral-0.5 fallbacks are weighted equally to real signals.**
  `_weighted_average` (`scoring.py:1044`) normalizes by present-component
  weight, but missing-data axes return 0.5 and keep full weight → scores
  drift to ~50 when SPY/weekly/RP/etc. data is absent. Combined with
  `min_score_to_alert`, sparse-data tickers get spuriously mid scores.
- 🟠 **VBP non-canonical** (`vbp.py:29` close×volume in close bin, not H-L
  spread) + leftover `# FIXED: missing parentheses` cruft (`:51,52,55,99,133`).
- 🟠 **`equity_curve.dollar_equity` formula self-cancels** (`:108`):
  `bankroll*r_unit_pct*kelly/r_unit_pct` → `r_unit_pct` has no effect. Dead
  (vulture) but mathematically wrong.
- 🟠 **Sharpe** (`stats_utils.py:347`) hardcodes "1R≈10%" for the rf
  conversion, contradicting the 1%-fixed-risk sizing (should be /0.01 not
  /0.10); treats monthly R-sums as %-returns with √12 annualization. Sortino
  divides by downside-count only (inflates vs target-downside-deviation).
- 🟡 **behavioral z-score** `(latest-mean)/std` unguarded for std==0
  (`behavioral/__init__.py:373`); uses full-series mean/std not rolling-52w
  (already a TODO item under Behavioral fetchers).
- 🟡 **Hardcoded scaling magics**: `_score_rs_exit` `*10` (`scoring.py:697`),
  `_score_bb_zscore` `/2.0` (`:776,779`); scoring hardcodes `rolling(50)/
  (200)` (`:328,329,353`) instead of `trend.ma_fast/ma_slow` → engine and
  scoring use different MAs under a `trend.ma_*` sweep.
- ⚪ **NIT**: ATR/RSI `ewm(adjust=False)` seeds on first value, not SMA-of-
  period like TA-Lib (warmup-only divergence); `rp_rank` needs 253 rows for
  the 12-mo term, not 252 (`rp_rank.py:47`).

### Phase 5 — configuration audit (2026-05-31)

- 🔴 **`signals.size_mult_gate` is documented as implemented but has ZERO
  code references.** README:211,284 ("blocks entries when composite size
  mult < min") and this TODO's Placeholder section ("✅ size_mult_gate (done):
  the gate itself was already implemented") both claim it works. `grep
  size_mult_gate` over the whole repo → only `filters.yaml`. The `enabled`
  + `min` keys are pure decoration; the macro/behavioral size gate does
    nothing. 5th phantom-guard instance.
- 🔴 **`scanner.vbp.{lookback,n_bins,volume_percentile}` orphan.** Defined in
  settings.yaml AND in `defaults.py`, but `_score_vbp_resistance`
  (`scoring.py:804,813`) hardcodes `compute_vbp(df, lookback=120, n_bins=24)`
  and `volume_percentile=70`. Values happen to match, so editing the config
  silently does nothing.
- 🔴 **`vix_high` inline default 25 ≠ config/DEFAULTS 28** at
  `filter_engine.py:726` (`rcfg.get("vix_high", 25)`). This is the EXACT bug
  `defaults.py` was created to kill — its own docstring (`:5-6`) cites
  `rcfg.get("vix_high", 25)` vs `vix_high: 28` as the motivating example —
  yet the fix was never applied here. `DEFAULTS` is imported by only 3 of
  ~10 consuming modules (behavioral, fred, regime); filter_engine/scoring/
  cache/main/fetcher still inline literals. Partial consolidation.
- 🟠 **`regime.vix_symbol` orphan** — never read; `main.py:_VIX_SYMBOL`
  hardcodes `^VIX`. Editing the config key has no effect.
- 🟠 **`config/messages.yaml` orphan file** — never loaded anywhere in code.
- 🟠 **Validation gaps** (`FilterEngine._validate_config`): checks presence+
  type of 21 `filters.yaml` keys only. NO range/sanity checks (rsi_min<max,
  min_atr<max, vix_low<high, min_rr>0, weights≥0); NO `settings.yaml`
  validation at all; NO `signals.*.short_entry` validation (a typo silently
  disables shorts via `if not cfg: return False`).
- 🟠 **`defaults.py` registry drifted from shipped YAML** (the registry that
  is supposed to be the single source of truth): `min_score_to_alert` 50
  (DEFAULTS) vs 70 (yaml); `expected_hold_days_low` 10 (DEFAULTS) vs 1 (yaml)
  vs `(10,15)` (`SignalResult` dataclass default) — three disagreeing sources.
- 🟡 **`expected_hold_days_low: 1`** in settings.yaml is almost certainly a
  typo (signal description would read "~1–20d hold"; code/DEFAULTS expect 10).
- 🟡 **Orphan macro axes**: `risk_on_weights` defines `policy_stance_ca` and
  `wcs_spread_state`, but neither is in `axis_weights` → zero weight, never
  contributes to `risk_on_score`.
- 🟡 **README weights list (16) vs settings.yaml (14)** — `insider_buying`
  and `short_interest` are documented as weight keys but absent from the YAML
  (off by absence, not by the claimed ConfigError guard — see Phase 4/README).
- 🟡 **Magic numbers with a config home that code ignores or lacks**: VBP
  120/24/70, scoring MA 50/200 (vs `trend.ma_*`), `gap_risk` 3.0 inline
  (`filter_engine:530`), `max_bars_since_cross` 3 inline (`:905`), dv20 window
  20 (`:372`), slope window 20 (`scoring:341,356`), earnings-buffer ×3
  (`scoring:428`), `_score_rs_exit` ×10, `_score_bb_zscore` /2.0, form4
  $250k/3-insiders, 252/21 trading-day constants.

### Phase 6 — error handling audit (2026-05-31)

No bare `except:` anywhere ✅. Custom hierarchy used consistently (FetchError
6×, ValidationError 9×, ConfigError 9×, InsufficientDataError 2×). DB writes
degrade gracefully; fetcher network failures log WARNING w/ context; fred/boc
never leak keys. Issues:

- 🔴 **Backtester swallows `engine.signal` exceptions at DEBUG → returns
  no-signal.** `backtester.py:563,666` and `_call_engine` catch broad
  `Exception`, log at **DEBUG**, and return `SignalResult(passed=False)`. At
  the default WARNING log level this is **invisible**: a systematic engine
  bug during a backtest/sweep silently suppresses signals → fewer/zero trades,
  read as "no edge" rather than "broken." Same for `scorer.enrich`
  (`portfolio_backtester.py:554,815` → entry_score 0). Severity is **inverted**
  vs the non-critical `ticker_health` tracker, which logs WARNING (`:508/256`).
  This is bug-hiding in the highest-stakes code (the +143R generator).
- 🔴 **`sweep.py:787 except Exception: pass`** — silent swallow of settings
  load + `_apply_settings_mutation`. A failed mutation runs the sweep cell
  with unmutated/default settings and records the result as a valid parameter
  variation. No log line at all → corrupts sweep validity (a parameter that
  errored looks like it was tested).
- 🟠 **Scoring sub-scores swallow → DEBUG → neutral 0.5** (`scoring.py`
  `_score_rs_entry/_rs_exit/_weekly_trend/_bb_zscore`, e.g. `:666-668`). A
  systematic sub-score bug returns 0.5 for every ticker invisibly; combined
  with equal-weighting of neutral fallbacks (Phase 4), it silently drags all
  scores toward ~50.
- 🟠 **`main.py` 12 broad per-stage `except Exception`** (fail-open pipeline).
  Defensible for a live scanner, but they catch programming bugs (KeyError/
  AttributeError) as per-ticker WARNINGs and continue → a global bug runs to
  completion and writes degraded `scan_results` to DB instead of halting.
- 🟠 **`StaleDataError` is dead** — defined + documented in the hierarchy,
  `raise`d 0 times (cache path uses boolean `is_fresh()`). Wire it in or remove.
- 🟡 **Log-level inconsistency**: critical `engine.signal` / `scorer.enrich`
  failures log DEBUG (hidden by default); non-critical `ticker_health` tracker
  logs WARNING (visible). Backwards.
- 🟡 **curl_cffi degradation inconsistent**: module guard (`fetcher.py:36`)
  logs a graceful "may fail" warning, but `_fetch_one:304` hard-imports
  curl_cffi per thread → if absent, **all** tickers fail, not "some TSX".

### Phase 7 — test suite audit (2026-05-31)

Ran the suite (PYTHONPATH=src, deps installed): **166 passed** — matches the
claimed count. Where tests exist they are **good quality**: exact-value
assertions (`apply_stop_fill == 97.0`, `compute_r long@target == 2.5`),
edge cases, parametrized property tests, synthetic-DataFrame fixtures (not
over-mocked). Problems are reproducibility and coverage:

- ✅ **DONE 2026-05-31** (was 🔴) **`pytest tests/` from repo root.** A root `conftest.py` now puts `src/` on the path —
  verified 166/166 from a clean root with no PYTHONPATH. Original finding: No
  `tests/conftest.py`, and `src/` is not on `sys.path` → `ModuleNotFoundError:
  No module named 'core'` at collection. README/TODO both say "pytest tests/
  from the repo root" — that only works in PyCharm (auto-marks `src` as a
  sources root). Verified. Fix: add a `conftest.py` that inserts `src/`, or a
  `pyproject.toml`/`pytest.ini` with `pythonpath = ["src"]`.
- ✅ **DONE 2026-05-31** (was 🔴) **`tests/` is now version-controlled** — removed `/tests/` from `.gitignore`. Original
  finding: The entire `tests/` dir was gitignored (`.gitignore: /tests/`). A fresh
  `git clone` has NO tests → the "test suite contract" (pytest green) cannot
  be honored from the repo. Combined with the conftest gap, the suite is
  effectively PyCharm-local.
- 🔴 **Headline-number math is UNTESTED.** Zero unit tests touch:
  `indicators.indicators` (ATR/RSI/MACD/BB), `stats_utils` (Sharpe/Kelly/
  drawdown/bootstrap/MC), `equity_curve`, `backtest.stats`, `regime`,
  `dataframe_validator` (OHLCV correction), `rp_rank`, `vbp`, `cache`/
  `json_cache`/`db`, `position_manager`. `scoring` is touched only via the
  short-flip tests — the long sub-score *values* are unverified. The +143R
  machinery has no exact-value coverage; the well-tested part (fill model) is
  not where the risk is.
- 🟠 **~50% of tests (~75 of 166) cover the SHORT path** — which is OFF in
  production and shown to have no edge (postmortem). Test investment is
  concentrated on the disabled feature, not the long-only engine that produces
  the headline number.
- ✅ **DONE 2026-05-31** (was 🟠) **`yfinance` import is now lazy** in `earnings_history.py` (moved into
  `fetch_earnings_dates_from_yfinance`), so offline backtester tests collect without it. Original finding: `yfinance`
  was a hard top-level import for offline backtester tests.
  `earnings_history.py:25 import yfinance` runs at module load, so
  `test_short_borrow` / `test_short_portfolio_guard` cannot even *collect*
  without yfinance, despite exercising cached-parquet backtests. Make it lazy.
- ⚪ The "3 deselected (live-network)" is a PyCharm-local marker; in a clean
  env all 166 pass (the fail-open fetcher tests pass precisely because there
  is no network → fail-open → empty, as asserted).

### Phase 8 — performance / memory audit (2026-05-31)

- 🔴 **O(n²)-per-ticker backtest hot loop.** `_call_engine` passes the growing
  slice `df.iloc[:T+1]` every bar (`backtester.py:540`), and the engine/scorer
  recompute **full-series** rolling/resample then take `.iloc[-1]`:
  `_classify_trend` MA(50)/MA(200) (`filter_engine.py:1161-1162`),
  `_market_regime` (`:1084`), `_sector_strength_ok` (`:832`), scoring
  `rolling(50/200)` ×4 (`scoring.py:328,329,339,353`), and the worst —
  `_score_weekly_trend` `df["close"].resample("W-FRI")` recomputed **every
  scored bar** (`:722`). Each is O(T) per bar → O(N²) per ticker, ×universe
  ×sweep-grid. **The fix is already half-built and dead:** `attach_indicators`
  precomputes `ma_fast`/`ma_slow`/`weekly_sma10` once (O(N)) but nothing reads
  them (Phase 2). Either read the columns (O(1)/bar) or at minimum slice-then-
  mean (`close.iloc[-w:].mean()`, O(w)) instead of full-series rolling. This is
  the dominant sweep cost and the reason `_pack_universe` exists.
- 🟠 **`sweep._pack_universe` pickles ~75 MB to each worker** (already in the
  Performance backlog) — memory scales ×`--workers`. Move to
  `multiprocessing.shared_memory`.
- 🟡 **`breadth.py:58`** does ~100 `cache_load` parquet reads per breadth
  computation (capped slice `[:100]`); once per run (24 h cached) so tolerable,
  but heavy and biased (Phase 5).
- 🟡 **`fetcher._fetch_one:305`** creates a fresh curl_cffi `Session` per ticker
  — no connection pooling/reuse across the ~90 watchlist fetches.
- ⚪ **`vbp.py:63`** Python accumulation loop is vectorizable (`np.add.at`);
  bounded to 120 bars so negligible.
- ✅ **No memory leaks found.** `_MACRO_STATE_CACHE`/`_BEHAV_STATE_CACHE` are
  bounded (size-cap eviction); `http._LIMITERS` keyed by a small fixed set;
  daily DataFrames are small (~6k rows) so full-load (no streaming) is fine;
  the trades list is bounded by trade count. The shared `FilterEngine._today`
  mutate/restore is not a leak but confirms the engine is not thread-safe.

### Phase 9 — security / privacy audit (2026-05-31)

Strong posture overall: **no `eval`/`exec`**, no hardcoded secrets, no
`yaml.load` (safe_load only), all SQL **parameterized** (named `%(...)s` in
`db.py`/`position_manager.py`/`backtest/db.py` — zero string interpolation),
`secrets.env` gitignored with placeholder-only `.example`, ticker input
charset-validated, fixed (non-user) URLs so no SSRF, `http._safe_url` strips
query strings before logging. `pickle` is used only in `sweep.py` for
in-process ProcessPool IPC of the app's own universe (not untrusted data) —
safe. Issues:

- 🟠 **F9-A log scrubber documented as active but never installed** (6th
  phantom-control instance). `config/secrets.env.example` reassures users
  "the codebase has a defensive log scrubber (F9-A)"; `http.py`'s docstring
  says install `mask_api_keys_filter()` at the root logger. The only
  `addFilter(...)` reference is *inside that docstring* — it is wired in
  nowhere. The FRED key is still reasonably protected by manual discipline
  (`fred.py` never logs `str(exc)`; `http._safe_url` strips the query), but
  the advertised defense-in-depth does not exist. Fix: actually install it in
  `main._setup_logging`, or drop the claim from the example file.
- 🟡 **`SEC_USER_AGENT`** is documented (README + secrets.env.example: "REAL
  contact email required by SEC fair-access policy") but **never read** —
  `form4.py` uses `yfinance.insider_transactions`, not direct EDGAR. Setting
  it does nothing; the EDGAR path it guards doesn't exist (TODO: Form 4 XML).
- 🟡 **FRED key travels as a URL query param** (`fred.py` `params={"api_key"}`)
  — unavoidable with FRED's API (no header auth), and mitigated by the two
  manual log guards above, but worth noting it can surface in any
  intermediary that logs full URLs.
- ⚪ External input is parsed defensively everywhere (float coercion,
  structural sanity guards, fail-open) and never executed — no injection/RCE
  vector. No PII beyond the user's own positions in their own MySQL; no
  telemetry/exfiltration. `main.py:868 __import__("datetime")` is a code
  smell, not a security issue.

### Phase 10 — architecture audit (2026-05-31)

No module-level import cycles ✅ (verified Phase 1). Layers mostly clean
(persistence/validators depend only on `exceptions`). Structural issues:

- 🔴 **`FilterEngine` god-class** — 1317 LOC, **39 methods**, and it is *also*
  the DTO hub: `ScanResult`/`SignalResult`/`MarketRegime` live here and are
  imported by 7 modules. It does config-load+validate, scan gates, signal
  (4 entry triggers, 6 exit triggers, long+short), regime classification,
  ticker-trend, sector gate, stop-date calendar, quarantine. Split into
  `ConfigLoader` + `Scanner` + `SignalEngine` (already in backlog) **and**
  extract the DTOs to a leaf `types` module (the orphan `types.py` was meant
  for exactly this but only half-did it).
- 🔴 **No DI — 20 independent `yaml.safe_load` calls across 12 modules.**
  `FilterEngine`, `SignalScorer`, `cache`, `json_cache`, `fred` (per-call!),
  `behavioral`, `calendar`, `chart`, `fetcher`, `main`, `sweep`,
  `run_backtest` each re-read config independently. An `ApplicationContext`
  (already in backlog) would parse once and inject.
- 🟠 **Magic strings pervasive (~190+):** `"long"` ×35, `"short"` ×31,
  `"mean_reversion"` ×19, `"exit_long"` ×18, `"momentum"` ×14, `"CHOP/BULL/
  BEAR"` ×30+. The typo-guard enums (`core.types.DIRECTION`/`SIGNAL_TYPE`)
  exist but are orphan (used in one test). Adopt them or delete them.
- 🟠 **Inverted/hub dependencies** (no cycles, wrong direction):
  `persistence/db → main` (`TickerResult`), `core/types → filter_engine`,
  and everyone → `filter_engine` for DTOs. DTOs belong in a leaf module.
- 🟠 **No `utils/` layer; cross-cutting helpers duplicated or misplaced:**
  cache-freshness logic copied ×9 (Phase 2), `_connect` ×3 (1/3 consolidated),
  `stats_utils` (pure math) lives under `backtest/`, `paths.py` (the intended
  shared-paths util) is orphan. A `CacheBackend` + a real `utils`/`io` layer
  would absorb these.
- 🟠 **Other god-modules for the split backlog:** `main.py` 906 (orchestration
  + CLI + persistence glue + reporting), `sweep.py` 1003, `report.py` 923,
    `scoring.py` 1094, `portfolio_backtester.py` 840.
- ⚪ **class-vs-function is mostly right**: indicators & fetchers as pure
  functions ✅; `FilterEngine`/`SignalScorer`/`TickerStore` as classes ✅. The
  duplicated fetcher cache helpers are the main "should be a shared class".

### ⭐ META-FINDING (spine of the whole audit)

The single dominant issue is not any one god-class — it is a **recurring
"phantom consolidation" pattern: a refactor or safety control declared done
in a docstring / README / TODO / requirements, but partial or absent in
code.** Nine+ confirmed instances:

1. `paths.py` — "all callers import from here" → 0 imports (P1)
2. `types.py` — "TickerResult moved here" → live dup still in `main.py` (P1)
3. `db_conn` — "all three call sites import from here" → 1 of 3 (P2)
4. `filelock` — requirements "hard-required" → never imported (P2)
5. `mask_api_keys_filter` / F9-A scrubber — "install in setup_logging" →
   never installed; cited as protection in secrets.env.example (P2/P9)
6. `size_mult_gate` — README+TODO "implemented" → 0 code refs (P5)
7. `scanner.vbp.*` config — defined + in DEFAULTS → code hardcodes 120/24/70 (P5)
8. `defaults.py` ("single source of truth") — adopted 3 of ~10 modules;
   `vix_high` still inline `25` ≠ DEFAULTS `28`, the *exact* bug it cites (P5)
9. precomputed `ma_fast/ma_slow/weekly_sma10` columns — written, read nowhere;
   they are the unbuilt fix for the O(n²) hot loop (P2/P8)
   (plus: claimed `ConfigError` weight guard absent (P4); `SEC_USER_AGENT`
   documented, unused (P9); `Trade.compute_r` docstring contradicts code (P4)).

This is a *process* problem — changes marked complete before wiring/verifying
(visible in this TODO's own "✅ done / VERIFY in PyCharm" cadence). It is the
root cause of the silent divergences found in Phases 2-9, and it matters most
because it makes the headline +143R **untrustworthy until independently
re-verified**: `size_mult` never reaches reported R (P4), gap-entry losses
zero out (P4), the loose sweep-tuned MR-long gate is unvalidated (P3), and the
backtester/sweep silently swallow engine errors (P6) — each individually
plausible, together they all bias the same direction (optimistic) and none is
caught by tests (the core math is untested, P7).

**Recommended remediation order** (do NOT start until the validation program
— frozen-universe A/B, OOS lock, deflated Sharpe — is run, since that decides
whether the edge is even real): (1) close the 9 phantom consolidations or
delete the dead halves; (2) add `conftest.py` + commit `tests/` + unit-test
the core math (indicators/stats/scoring values); (3) decide `effective_r` vs
`r_multiple` for reporting; (4) raise the swallowed backtester/sweep
exceptions to WARNING; (5) only then the FilterEngine/main/sweep splits + DI.

### Cleanliness census (2026-05-31) — comments/prose/dead code

Code is NOT clean of stale prose. No commented-out code blocks ✅, but:

- 🟡 **9 `# FIXED: ...` scar comments** — leftover from a past parenthesis/
  indentation fix; document a transient editing mistake, not behavior. Remove.
  `vbp.py:51,52,55,99,133`, `indicators.py:226`,
  `chart_signal_history.py:100,110,117`.
- 🟡 **41 internal ticket-ref comments** (`P0-4`, `P0-6`, `P1-7`, `F3-1`,
  `MINOR-02`, `Phase 10.3` …) — meaningless without the chat/commit log;
  densest in `portfolio_backtester.py` (18), `sweep.py` (6), `scoring.py` (4).
  Replace each with the actual reason, or drop.
- 🟡 **22 changelog-in-comments** ("previously…", "was 45 — widened",
  "lowered from 0.18", "reworked 2026-05-30", "done concurrently by another
  agent") across ~16 files — narrate history, not current behavior. `filters.
  yaml` is the worst (config the user edits doubles as a sweep changelog:
  "widened from 65", "sweep best: +0.410 R / 102 trades").
- 🟡 **Confirmed dead functions/methods (0 refs outside their own def):**
  `loader.available_tickers`, `stats_utils.binomial_p`,
  `stats_utils.monthly_r_series`, `filter_engine.set_quarantine`,
  `ticker_health.snapshot`, `equity_curve.dollar_equity` (also math-broken,
  P4). `nearest_high_volume_node_below` (vbp) has 1 ref — verify/likely dead.
  (`BarReplayBacktester` and `stats_utils.max_drawdown` are live — vulture
  false positives.)
- ⚪ **20 `# noqa`** are mostly legitimate (E402 for the sys.path-before-import
  dance; BLE001 for deliberate broad-excepts).
- ⚪ This belongs to the existing "Naming, docs, comments (cleanup pass)" +
  "Placeholder / dead / orphan code" blocks — fold these concrete line refs in.

**Audit completeness note:** correctness-critical code was fully covered.
`chart.py` and `report.py` were deep-read last (gap-close 2026-05-31):

- 🟠 **`chart.py` draws a misleading "oversold" RSI line.** `_load_rsi_
  thresholds` (`:53-64`) takes the chart's oversold band from
  `mean_reversion.long.rsi_max` (fallback 35) — but that key was widened to
  **70** (Phase 3/5), so the chart paints an "oversold" line at RSI 70
  (overbought territory). Also a module-import-time config read.
- ⚪ **`report.py` is presentation-only** — reads already-audited `Stats`/
  `EquityCurve`, sums `t.r_multiple` (confirms the effective_r gap, P4), and
  uses 1%-risk dollar conversion in `print_kelly`/`print_mc_drawdown` while
  `stats_utils.sharpe` uses 1R≈10% (confirms the P4 sizing inconsistency from
  another angle). No misleading new computation.
- ✅ Fixed the two zero-brace f-strings (`report.py:160,265`). Left the JS-
  template / multi-line ones (`report.py:448`, `run_backtest.py:169,284`):
  escaped `{{ }}` braces make a mechanical `f`-strip unsafe — handle by hand. `walk_forward.py` was spot-checked: IS/OOS
  windows use properly
  separated date ranges (`is_start/is_end` vs `oos_start/oos_end`) with a
  degradation metric — structurally sound; the `re_tune` (tune-on-IS, test-on-
  OOS) path is the only place a subtle leak could hide and deserves a dedicated
  read if/when the validation program is run.

---

## Validation & de-biasing program (TOP PRIORITY — the "is the edge real" work)

Design: `docs/validation_program_design.md`. **Strategic pivot (2026-05-30):**
the engineering is sound; the open question is whether the +143R edge is real
or an artifact of universe/parameter selection. Edge is a *measurement*
problem — these phases mostly try to *shrink* the headline until only the
trustworthy part remains. **Short-side tuning is paused** until the long-only
edge is validated (shorts already shown non-additive — see postmortem).

Sequenced; each gate must pass before the next earns effort:

- **Phase A — survivorship / selection audit** (focus: tier_a hand-picked):
  - ✅ **A0 symbology normalizer** (done 2026-05-30): `src/core/fetchers/symbology.py`
    `to_yf_symbol()` maps `ABC.DE.TO`→`ABC-DE.TO`, `BRK.B`→`BRK-B`, preserves
    exchange suffixes, identity for clean symbols; wired into `yf_fetchOne.py`
    (cache key stays the original). 16 tests. Resolves "dead tickers" that
    were really mis-symboled — re-run the fetch in PyCharm to confirm the
    universe grows.
    ⚠ **AUDIT 2026-05-31 — A0 is only 1/8 wired.** `to_yf_symbol` is called
    *only* in `yf_fetchOne.py:81` (prices). The other six yfinance call
    sites take the raw ticker: `info_fetcher.py:87`, `live_price.py:73`,
    `earnings_history.py:98`, `behavioral/form4.py:90`,
    `behavioral/short_interest.py:73`, `macro/yf_macro.py:65`. So for
    `BRK.B`/compound-TSX symbols, prices load but market-cap, earnings,
    insider, short-interest and live-price silently return None/[] — and
    fail-open hides it. A0 is NOT done for the fundamentals/behavioral path.
  - ◻ **A1 inception-aware frozen-universe A/B**: a `watchlist.frozen_universe`
    block (as-of date + only names that existed/were obvious then, no
    backtest-pruned re-adds) + run full-hindsight vs frozen and report the
    **selection discount** (Δ total R / Sharpe / DD + count of look-ahead
    inclusions where first_bar > as_of). Free, no external data. This is the
    number that tells us how much of +143R was hindsight.
- **Phase B — realistic frictions**: costs/slippage/borrow ON by default;
  re-measure. (Existing entry_slippage + borrow plumbing; flip defaults + sweep.)
- **Phase C — locked out-of-sample**: pick params on ≤2015, lock, test
  2016-2026 exactly once; make walk-forward the headline, retire the
  in-sample baseline as a marketing number.
- **Phase D — multiple-testing correction**: deflated Sharpe / White reality
  check / haircut for the number of configs tried (sweeps inflate the "best").
- **Phase E — live reconciliation**: paper-trade 6-12 months, reconcile fills
  to backtest (the live-vs-backtest journal in Reporting). The real judge.

Data-source decision (researched 2026-05-30): tier_a needs **no** membership
feed (ETFs have no index membership — inception-aware frozen A/B is the right
free fix). tier_b later (if pursued): free date-stamped S&P 500 constituents
CSV + the existing Wikipedia scrape; paid (Norgate/Sharadar) only if expanding
to full single-stock survivorship-free universes.

---

## Execution plan (sequenced, 2026-05-28)

Goal: land Short Trading end-to-end (10.4 → 10.5 → 10.6) before touching
the named backlog sections. Each step ends green (`pytest tests/`) and
bit-identical baseline (111 trades · +33.07R) with shorts OFF. Do the
steps in order; later sections are sequenced after, lowest-risk first.

**Repo reality check (verified 2026-05-28, adjust the TODO accordingly):**

- 10.4 is further along than the table said. `scoring.py` already has
  `_flip_if_short`, `_FLIP_FOR_SHORT_ENTRY/EXIT`, direction-aware
  `_score_entry`/`_score_exit`, **and `enrich()` already passes
  `signal.direction` through** (lines 202-216). The old "needs flip in
  enrich() only" note was stale — that wiring already existed.
- ✅ `data/backtest_out/short_trading_design.md` restored 2026-05-28
  (the pointer at the top of Active work is valid again).
- ✅ `tests/test_short_scoring.py` and the rest of the short-trading
  suite are in and green — full suite **163 passing / 3 deselected**
  (166 collected) as of 2026-05-29.
- ✅ `--allow-shorts` is wired into `main.py` (10.5 done); the stdout
  summary now has SHORTS (entries) + COVERS (held-short exits) blocks.
- ⏳ Regression run (`run_backtest.py --no-html --no-csv`, expect
  111 trades / +33.07R) and the 10.6 BEAR-window `validate_shorts` run
  remain **pending in PyCharm** — the sandbox has no `yfinance`.
- `report.py` still has no "By direction" block — the terminal summary
  breaks out shorts, but the HTML/report by-direction table is a separate
  open item (see Reporting & observability).

### Step A — Close out 10.4 (scoring direction-flip)

1. Restore the design doc: write `data/backtest_out/short_trading_design.md`
   capturing the 10.1-10.6 design + the v1 cuts (MR-short geometry is
   asymmetric; `1 - score` flip is momentum-correct, MR-short deferred to
   v2). Fix the pointer at the top of Active work.
2. Read `src/core/scoring.py` around lines 237-296 (`_flip_if_short`,
   flip lists) and 293-525 (`_score_entry`/`_score_exit`) and confirm the
   diff matches intent.
3. Write `tests/test_short_scoring.py`:

- bullish synthetic df → `_score_entry(direction="long")` high on
  `trend_up`/`breakout_20d`/`macd_bullish`; `direction="short"` low.
- bearish synthetic df → mirror image.
- `_flip_if_short` is a no-op for `direction="long"`.
- `enrich()` on a `direction="short"` signal dispatches the flipped
  path and components are inverted.

4. Document the v1 cuts in the `_flip_if_short` docstring.
5. Regression gate: `--no-html --no-csv` baseline run must stay
   111 trades / +33.07R. Run `pytest tests/`; record new test count
   (update the State-of-play and Test-suite-contract numbers).

### Step B — 10.5 (CLI + end-to-end)

1. Add `--allow-shorts` flag to `main.py` (default OFF; defers to
   `signals.allow_shorts` config). Keep baseline bit-identical when unset.
2. `main.py` / `print_baseline` summary: break out long vs short
   (groundwork for the "By direction" reporting item).
3. Smoke test on synthetic BEAR data; add a small e2e test.
4. README sync: new `--allow-shorts` flag, any new config block, test
   count. Run full suite green.

### Step C — 10.6 (postmortem-style validation)

Needs a window with real BEAR regimes (cached 2023-2026 is bull-only —
extend dataset or temporarily relax the regime classifier; note which).
Run the six checks in *Short Trading — validation checks* below: trade
count by direction (≥10 shorts/3yr), R-distribution symmetry, win rate
by side (≤10pp gap), Sharpe/Calmar shorts-on-vs-off (≥ flat), by-exit
breakdown (short stop-rate < 40%), and no concurrent long+short on the
same ticker. Capture results in a short postmortem doc.

### After Short Trading lands — backlog order

Sequenced lowest-risk/highest-leverage first; each is detailed in its
named section below:

1. **Short Trading v2 polish** — borrow cost, HTB list, asymmetric
   `min_rr_short`, proper MR-short mirror functions.
2. **Fetch Errors Fix/Data fullfillness check** — ◐ mostly DONE 2026-05-30
   (see "Fetch-error hardening" in State of play). BoC 404 fixed (live IDs),
   AAII/NAAIM/COT now fail-open-quiet with live fallbacks, divide-by-zero
   confirmed already-gone. **Left:** verify COT resource ID `s9da-n2w9`, run a
   clean PyCharm fetch to confirm the log is traceback-free, and do a formal
   per-axis confidence audit of how calculations gate on missing data.
3. **Naming / docs / comments cleanup pass** — hygienic; the TODO marks
   it "should land before next-phase architecture work.", all the prosa stuff must be gone.
4. **Placeholder / dead / orphan code sweep** — quick wins, de-risks the
   architecture split. ```I NEED A CAREFULL CHECK WHAT WAS INPLEMENTED BUT BROKEN```.
5. **Backtester fills & entry geometry** — no-look-ahead test, open-EOD
   regression, slippage stress test (correctness guards).
6. **Scoring system** — sub-score audit, VBP rewrite, keep weight guards.
7. **Robustness / postmortem follow-ups** — walk-forward joint-optimal,
   chronic-loser production wire, defensive sleeve, per-ticker stops.
8. **Behavioral / macro fetchers** — Form 4 XML, AAII/NAAIM audits,
   z-score window, survivorship in constituents.
9. **Reporting & observability** — stand-down log, by-direction summary,
   live-vs-backtest journal, Telegram alerts.
10. **Watchlist expansion** — +~15 Canadian ETFs (run each through the
    chronic-loser filter + 5yr-history check).
11. **Architecture refactors** — FilterEngine / main.py / sweep.py
    splits, DI context, sector caps. (Deferred until cleanup lands.)
12. **Performance** — measure first; shared-memory pack, cache-key hash.
13. **Operational** — retroactive opens, pin requirements, schema export.

**Standing rules for every step:** README sync after each landed change;
`pytest tests/` green at end of each step; update State-of-play numbers.

---

## Active work — Short trading (multi-step)

Design doc: `data/backtest_out/short_trading_design.md` *(missing —
recreate as Step A.1 of the execution plan above)*.

| Step | Title                                 | Status                                         | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
|------|---------------------------------------|------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 10.1 | Plumbing (types, Trade, fill helpers) | **done**                                       | `sign_of()`, `Trade.compute_r` sign-aware, `apply_*_fill_short` helpers, `MarketRegime.allows_shorts`, `position_CLI --side short`. 27 tests.                                                                                                                                                                                                                                                                                                                                                                           |
| 10.2 | Signal triggers + held-short exit     | **done**                                       | `_momentum_short_entry`, `_mean_rev_short_entry`, `_signal_exit_short`, `signal(held_short=…)` dispatch. 18 tests.                                                                                                                                                                                                                                                                                                                                                                                                      |
| 10.3 | Backtester wiring                     | **done**                                       | Both `BarReplayBacktester._walk` and `PortfolioBacktester.{run_all,run_prepped}` direction-aware. 3 integration tests.                                                                                                                                                                                                                                                                                                                                                                                                  |
| 10.4 | Scoring layer direction-flip          | **done***                                      | `_score_entry(direction=…)` + `_flip_if_short` + `_score_exit` flip all wired; `enrich()` already passes direction through. Tests added (`test_short_scoring.py`, 9). *Regression run pending in PyCharm (sandbox lacks yfinance) — no production code changed this pass, so baseline is unaffected by construction.                                                                                                                                                                                                    |
| 10.5 | CLI + end-to-end                      | **done**                                       | `main.py --allow-shorts` sets `signals.allow_shorts`; summary adds SHORTS (entries) + COVERS (held-short exits) blocks; `test_short_e2e.py` smoke fires a real short via the public `signal()` API on synthetic BEAR data (2 tests). README synced. Full BEAR-window backtest validation is 10.6.                                                                                                                                                                                                                       |
| 10.6 | Postmortem-style validation           | **validated 2026-05-30 — shorts NOT additive** | Ran shorts-on vs long-only over 2001-2026 (`validate_shorts.py`). Plumbing sound (stop symmetry −1.04R, stop-rate 12%, concurrency safe) but **no edge**: +99 shorts netted ≈ −1.6R (WR 32.3% vs 46.9% long), Sharpe 0.62→0.57, max DD 37.6→38.5R, recovery 1143→1981d, MC p95 DD breaches 25%. **Keep `signals.allow_shorts: false` in production.** Full write-up: `postmortem_2026-05-30.md` § Short validation. Next: tighten short-entry selectivity / gate MR-shorts off, re-run until check 4 is flat-or-better. |

### Resume notes for 10.4

The 10.4 work was paused mid-flight after a manual section-header
rewrite corrupted scoring.py. The file now compiles (one mangled
`── sub-score helpers ──` divider was repaired) and the cumulative
test suite is green. What's still missing for 10.4:

- **Verify**: read `src/core/scoring.py` lines around the new
  `_flip_if_short` helper and the modified `_score_entry`/`_score_exit`
  signatures to confirm the diff matches the intent.
- **Add unit tests** under `tests/test_short_scoring.py`:
  - Synthetic bullish df → `_score_entry(direction="long")` gives high
    `trend_up`, `breakout_20d`, `macd_bullish`; `direction="short"` on
    same df gives LOW for those (flip works).
  - Synthetic bearish df → opposite shape.
  - Confirm `_flip_if_short` is a no-op for `direction="long"`.
  - `enrich(signal_with_direction_short)` dispatches through the new
    path and components are flipped.
- **Document** the v1 cuts (MR-short geometry is asymmetric; polish
  follow-on in `Short Trading — v2 polish` below).
- **Regress** baseline `--no-html --no-csv` run; must remain
  111 trades / +33.07R.

### Short Trading — validation checks (Phase 10.6)

Checks 1-5 are now automated in `backtest/validate_shorts.py` (reads the
trade ledger); check 6 is locked by `tests/test_short_portfolio_guard.py`.
What's left is to **produce a ledger over a real BEAR window** and run
the harness in PyCharm:

```
python -m backtest.run_backtest --allow-shorts --no-html --start <bear-start> --end <bear-end>
python -m backtest.validate_shorts data/backtest_out/trades.csv --baseline <long-only-trades.csv>
```

The cached 2023-2026 data is bull-only; use older data or temporarily
relax the regime classifier so shorts fire. The checks (for reference):

- **Trade count** by direction. Goal: ≥ 10 short trades in a 3-year
  window with `--allow-shorts`. If zero shorts fire, the regime gate
  is masking the test — relax temporarily or extend dataset.
- **R-distribution symmetry**: short stop-out r-multiple distribution
  must mirror long (centred near -1, similar tail). If shorts cluster
  at -2R+ the gap-fill geometry is wrong.
- **Win rate by side**: if shorts WR < longs by >10pp on a balanced
  window, the trigger thresholds are too aggressive.
- **Calmar / Sharpe with shorts on vs off**. Acceptance: Sharpe
  improves or stays flat with shorts on (shorts are insurance, not
  alpha).
- **Postmortem-style by-exit breakdown** (`stop / target / engine /
  open_eod`). Stop-rate for shorts must be < 40% (matches longs).
- **No accidental concurrent long+short on the same ticker** —
  `PortfolioBacktester` should refuse to open a short while a long is
  held on the same symbol (and vice versa). Verify with a deliberate
  same-ticker test.

### Short Trading — v2 polish (after 10.5/10.6 land)

- ◐ **Borrow cost** (mechanism done 2026-05-28): `Trade.borrow_drag_r()`
  folds a per-trade R drag into `effective_r` (shorts only; default 0 →
  baseline-safe). `PortfolioBacktester` reads `signals.borrow.{annual_rate_default,
  per_ticker}` and stamps the rate per short entry. Tests in
  `tests/test_short_borrow.py`. **Remaining**: v1 uses a flat configured
  rate — wiring a *live* per-symbol borrow source (IBKR API / SLB rates
  page) is the follow-on, and the BarReplayBacktester single-ticker path
  isn't borrow-aware yet (portfolio path is).
- ✅ **HTB / availability check** (done 2026-05-28): `signals.hard_to_borrow_list`
  config block; `_signal_entry` blocks short entries on listed symbols
  (longs unaffected). Tests in `tests/test_short_v2.py`.
- ✅ **Asymmetric `min_rr_short`** (done 2026-05-28):
  `signals.stop_loss.min_rr_short` overrides `min_rr` for shorts only;
  absent → falls back to `min_rr`. Tests in `tests/test_short_v2.py`.
- ◐ **MR-short geometry** (signal_type-aware flip done 2026-05-28):
  `_score_entry` now uses `_FLIP_FOR_SHORT_ENTRY_MR` for
  `signal_type=="mean_reversion"` shorts, which keeps `near_52w_high` and
  `far_from_52w_low` long-style (MR shorts fade strength near highs).
  Momentum shorts flip them. Tests in `tests/test_short_mr_flip.py`.
  **Remaining (optional)**: full mirror functions (`_score_near_52w_low`,
  `_score_breakdown_20d`) instead of the `1 - score` approximation.
- **Inverse-ETF avoidance**: don't short SQQQ when shorting QQQ is
  cheaper. Watchlist-policy concern, not code.

---

## Scoring system

Files: `src/core/scoring.py`, `src/core/defaults.py`,
`config/settings.yaml`.

- **Direction-flip tests** (Phase 10.4 leftover — see *Resume notes*).
- **Sub-score audit**: `_score_rs_entry` and `_score_rs_exit` already
  branch on direction implicitly via `signal_type`; verify they
  produce sane scores when called with `signal.direction == "short"`.
- **VBP rewrite**: `compute_vbp` puts `close × volume` in the close
  bin (not classic H-L spread). Either rename the function or rewrite
  to the canonical Volume-by-Price algorithm.
- **`scanner.weights.insider_buying` and `scanner.weights.short_interest`
  must remain 0** until Form 4 XML parser and the live short-interest
  pipeline are validated. Scorer construction still raises ConfigError
  when either weight is > 0 — keep that guard.

## Backtester fills & entry geometry

Files: `backtest/backtester.py`, `backtest/portfolio_backtester.py`,
`backtest/trade.py`.

- ✅ **No-look-ahead regression test** (done 2026-05-28):
  `tests/test_no_lookahead.py::test_engine_never_sees_a_future_bar`
  spies every `engine.signal` call in a real walk and asserts the frame's
  last bar == `engine._today` (no future row) and that slices grow one bar
  at a time. Locks the layering invariant.
- ◐ **Open-EOD count regression**: the *mechanism* is now locked —
  `tests/test_no_lookahead.py::test_open_position_force_closes_open_eod_on_last_bar`
  confirms an open position force-closes as `open_eod` on the last in-window
  bar. **Remaining (PyCharm)**: confirm the 4 baseline `open_eod` trades
  are genuine end-of-data closes against real cached data.
- **Slippage stress test** (PyCharm): rerun `--start 2023-01-01` at
  `entry_slippage_pct ∈ {0, 0.002, 0.003}` and confirm Total R degrades
  smoothly. Unit-level fill/slippage math is already locked by
  `tests/test_fill_models.py`; only the full-backtest monotonicity needs
  the real run.
- ✅ **Same-bar-stop pessimism** (done 2026-05-28): documented in
  `Trade.compute_r`'s docstring — when a bar's H/L spans both stop and
  target, the backtester records the stop (worse) fill; deliberate, not
  a bug.

## Robustness / postmortem follow-ups

## Watchlist expansion

- **Pull more Canadian (.TO) tickers** into tier_a to reduce
  currency-conversion + exchange fees on cross-border trades. Target
  composition: keep ≤ 20% individual stocks (current rule), expand
  Canadian ETF coverage:
  - Sectors: XIT (tech), XFN (financials), XEG (energy), XGD (gold),
    ZWB (covered calls), CDZ (dividend aristocrats).
  - Style/factor: XSP (S&P 500 hedged CAD), VFV (S&P 500 unhedged),
    XIU (TSX 60), XIC (TSX composite — already in).
  - Themes: TXF (tech covered call), HCAL (Canadian banks 1.25×).
    Aim for ~15 additional Canadian symbols. Verify each has > 5 years
    of price history and survives the chronic-loser filter.

## Behavioral / macro fetchers

Files: `src/core/fetchers/behavioral/*.py`, `src/core/fetchers/macro/*.py`,
`src/core/macro/calendar.py`.

- **Form 4 XML parser**: yfinance's `insider_transactions` is text-
  matched on "Sale"/"Purchase". Replace with direct SEC EDGAR XML
  parse to distinguish `P` (open-market purchase) from `S`
  (open-market sale) and aggregate exact $ values. SEC_USER_AGENT env
  var required.
- **AAII live fetch** ✅ reworked 2026-05-30: the `.xls` download is
  permanently 403 for bots, so `aaii.py` now primes session cookies on the
  homepage then scrapes the `sent_results` HTML table (`_parse_aaii_html`,
  percentages normalised to [0,1]); fails open to cache/empty. **Verify in
  PyCharm** the live page still exposes a Bullish/Bearish table (layout drift
  is the remaining risk).
- **NAAIM URL** ✅ reworked 2026-05-30: the 2014-vintage `.xls` path 404s.
  `naaim.py` now loads cached history and scrapes the latest index value from
  `naaim.org/resources/naaim-exposure-index/` (regex over the page), appending
  the new weekly point; fails open to cache. **Verify** the regex still
  matches the live page format in PyCharm.
- **CFTC COT resource ID** ⚠ changed 2026-05-30: `cot.py` `_COT_URL` moved
  `72hh-3qaq` → `s9da-n2w9`. Confirm this is the intended Disaggregated
  dataset at `publicreporting.cftc.gov/api-docs/` (web research pointed at
  `72hh-3qpy` for Disaggregated Futures-Only) — a wrong ID silently returns
  a different dataset. Fails open-quiet on 404/rate-limit.
- **Behavioral sentiment z-score**: should use rolling 52-week, not
  full-series. Update `_classify_sentiment` in
  `src/core/behavioral/__init__.py`.
- **`compute_sp500_breadth` constituent truncation**: hard-coded to
  first 100 alphabetical names. Either thread the full universe or
  document the bias.
- **Survivorship in constituents fetchers**: `sp500_constituents.py`
  and `tsx60_constituents.py` pull *current* membership. Add
  date-stamped historical membership + a `as_of` filter. Without
  this, 2018-vintage backtests silently include 2026-membership names.
- **FOMC / CPI live scrape**: `calendar.py` ships with a hard-coded
  2026 list. Add a scraper for `federalreserve.gov/monetarypolicy/
  fomccalendars.htm` so the next-year calendar refreshes automatically.

## Reporting & observability

Files: `backtest/report.py`, `backtest/equity_curve.py`, `main.py`.

- **Stand-down log**: silent regime periods (e.g. Mar-May 2025) should
  appear in the report as a separate block, not vanish from the monthly
  Series. Currently mitigated by the zero-trade-month backfill in the
  bar chart; do the same for the summary stats.
- **Per-direction breakdown** in the terminal `print_baseline`
  summary: once shorts land, add "By direction" alongside "By
  signal", "By regime", "By exit", "By year".
- **Live-vs-backtest reconciliation journal**: capture daily fired
  signals + a 30-day-forward outcome flag. Compare live execution to
  the backtester's prediction for the same signal/date. Drift >
  ±0.15 R per trade triggers an alert.
- **Telegram alerts**: `TG_CHAT_ID` and `TG_BOT_TOKEN` env vars are
  reserved (README) but not wired. Send a message when a high-score
  entry fires or when a held position hits its exit signal.

## Naming, docs, comments (cleanup pass)

This block is purely hygienic — should land before next-phase
architecture work.

**Audit 2026-05-28** narrowed this block considerably:

- ✅ **`# TODO` / `# FIXME` audit**: `grep -rn "# *TODO\|# *FIXME"` over
  `src/ backtest/ main.py position_CLI.py` returns **0 hits** — there are
  no stale inline TODO/FIXME comments to close. Item done.
- If the magic-strings rule still matters, re-state it somewhere
  live (README) — otherwise this item is N/A.
- **Phase numbers → named blocks** (low priority): 61 `Phase N` refs
  remain across 10 files, but the breakdown is mostly recent, meaningful
  `Phase 10.x` (10.2–10.6) plus a handful of old `Phase 1/6/7`. Only the
  pre-10 ones are "meaningless without the chat log"; suggest renaming
  just those when next touching each file rather than a churny sweep of
  big files (filter_engine.py 1317 LOC, report.py 923 LOC).
- **Module docstring pass** (low priority): nearly every module already
  has a top docstring; what's missing is the "Public API" / "Consumed
  by" sections on ~25 files. `position_manager.py` and the large
  `filter_engine.py` / `report.py` are the sparsest. Bounded but
  unverifiable (comment-only) — best done opportunistically per-file.

## Placeholder / dead / orphan code

Run a sweep to find and either wire up or remove:

- ◐ **Unused local symbols** (audit 2026-05-28): removed `trough_eq`
  (equity_curve.py) and `distinct_30` (scoring.py) — both assigned but
  never read; `breadth_records` no longer exists in the tree. The
  "11 instances" figure was a stale PyCharm count; a fresh `grep`-style
  sweep of the remaining 8 (re-run PyCharm's "unused local" inspection)
  is the only leftover.
- ✅ **`scanner.chart.scorecard_panel`** (removed 2026-05-28): was a dead
  knob — defined in `defaults.py` + `settings.yaml`, read nowhere. Deleted
  from both. (If a scorecard panel is wanted later, add it as a fresh
  feature with a live consumer in `chart.py`.)
- **`signal.expected_hold_days`** — set by scorer, consumed only in chart
  text (`chart.py`) and the description line. Driving a **max-hold exit
  rule** off it is a *feature* (new engine/backtester exit), not cleanup —
  promoted to the Backtester/Scoring backlog rather than dead-code.
- ✅ **`signals.size_mult_gate`** (done): now documented in README's
  filters.yaml config table; the gate itself was already implemented.
- ✅ **`_PHASE_MODULES_AVAILABLE`** (documented 2026-05-28): not dead —
  it's an active import guard used at 4 sites so a partial/stripped
  install degrades to long-only core instead of crashing at import. Added
  an explanatory comment at its definition in `main.py` explaining why it
  stays (chose "document" over "remove").
- ✅ **Old "audit-tracker" tokens in `tests/test_regression_fixes.py`**:
  the file has been **deleted** from the tree (`git status: AD`), so the
  stale tokens are gone.
- ✅ **`backtest/db.py` MySQL schema** (done 2026-05-28): created
  `data/backtest_schema.sql` (the module docstring already pointed at it
  but the file was missing). `CREATE TABLE` for `backtest_runs` +
  `backtest_trades`, column lists mirrored from the INSERT SQL — single
  source of truth for a fresh deploy.

## Architecture (defer until cleanup pass lands)

- **FilterEngine god-class** (1300+ LOC after Phase 10): split into
  `ConfigLoader` + `Scanner` + `SignalEngine`.
- **main.py god-module** (800+ LOC): split orchestration / persistence
  / CLI / reporting.
- **backtest/sweep.py** (~1000 LOC): split engine / grid / worker /
  result.
- **`ApplicationContext` for DI**: each component currently re-reads
  YAML at construction.
- **`max_concurrent_per_sector`** in `PortfolioConfig`: consult
  `config/sector_map.yaml` so the portfolio can't end up 80% tech.

## Performance (low priority — measure before optimizing)

- **`_pack_universe` pickle cost**: 75 MB × N workers per sweep. Move
  to `multiprocessing.shared_memory`.
- **Walk-forward sweep cache key**: include parameter-grid hash so
  cache hits don't return stale results.

## Operational

- **`position_CLI.py open --date YYYY-MM-DD`** for retroactive opens.
- **`requirements.txt`** is unpinned. Pin transitive deps once the
  next clean release is ready.
- **MySQL schema export** (see Placeholder/dead/orphan section above).

---

## README sync (after every step)

After landing any code change above, update `README.md` to reflect:

- New CLI flags or env vars
- New config blocks added to `filters.yaml` or `settings.yaml`
- New tests or test counts
- Renamed entry points
- Any change to "Cold start" or "Entry points" sections

Aim: a fresh clone + `pip install -r requirements.txt` + read README
should be enough to run the baseline. Anything that breaks that
contract is a README bug.

---

## Test suite contract

`pytest tests/` must remain green at the end of every step. Live-
network tests (`test_cot_fail_open`, `test_aaii_fail_open`,
`test_naaim_fail_open`) are deselected in the sandbox; they should
pass in the user's PyCharm. Cumulative count after Phase 10 v2:
**163 passing** (166 collected − 3 deselected).
