# TradAlert

Swing-trading scanner and backtester. Momentum + mean-reversion entries,
held-long exits, portfolio-aware bar-replay, OFAT / walk-forward sweeps.

## Layout

```
main.py                Live scan: fetch → indicators → scan → signal → score
position_CLI.py        Manual position CRUD
telegram_bot.py        Interactive Telegram daemon (owner-only commands + buttons)
backtest/run_backtest  Backtester: baseline, sweep, walk-forward, robustness
backtest/repair_parquet Cross-platform parquet re-save utility

config/
  filters.yaml         Scan + signal + regime + stop-loss + size-mult gate
  settings.yaml        Scoring weights, thresholds, macro/behavioral, storage
  watchlist.yaml       Two-tier ticker universe (tier_a tradeable, tier_b RP-only)
  sector_map.yaml      Optional ticker → sector ETF mapping for sector_gate
  secrets.env          Local secrets (gitignored; see secrets.env.example)

src/core/              Domain: filter_engine, scoring, ticker_store, types, paths, defaults
src/core/fetchers/     yfinance OHLCV, FRED/BoC macro, behavioral (COT/NAAIM/AAII/breadth/Form4/short)
src/core/indicators/   ATR/RSI/MACD/Bollinger + VBP + chart renderer
src/core/macro/        Macro regime classifier (risk_on_score, size_multiplier)
src/core/behavioral/   Behavioral regime classifier (breadth, sentiment, positioning)
src/core/validators/   OHLCV + ticker validation
src/persistence/       Parquet cache, sectioned-JSON cache, MySQL persistence
backtest/              Sweep engine, walk-forward, stats, equity curve, report
tests/                 Regression suite (`pytest tests/`)
```

## Cold start

```bash
# Python ≥ 3.10
pip install -r requirements.txt

cp config/secrets.env.example config/secrets.env   # then fill in real values
python main.py --force                              # warms data/prices/* cache

pytest tests/                                       # run the test suite (conftest.py puts src/ on the path)

# (optional) create MySQL tables if you want SQL journaling; table definitions
# are inline in src/persistence/db.py, backtest/db.py, src/core/position_manager.py
```

If `signals.sector_gate.enabled: true` in filters.yaml, `config/sector_map.yaml`
must exist (an empty `sector_map: { }` ships by default).

## Entry points

### `main.py` — live scanner

```bash
python main.py [--force] [--allow-shorts] [--scoring]
```

| Flag             | Default | Description                                                                                                                                          |
|------------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--force`        | False   | Bypass cache staleness check; re-fetch every ticker.                                                                                                 |
| `--allow-shorts` | False   | Enable short-side entries. Sets `signals.allow_shorts=true` in the loaded filters config. Off keeps the long-only baseline replay-stable. |
| `--scoring`      | False   | Enrich + score signals and apply the `min_score_to_alert` gate. **OFF by default** — the entry score is non-predictive of R (corr −0.03), so every engine-triggered fire is reported as actionable instead of being score-gated (ADR-003). |

Outputs: stdout report, `data/screenshots/{TICKER}_{Dmonyy}.webp` charts for
fire-signals (date-stamped, e.g. `URA_4jun26.webp`, so daily shots don't overwrite),
`data/tradealert.log`, MySQL `scan_runs` + `scan_results` (when DB env set).
Each fire chart carries an **entry-gate "trigger panel"** sidebar — the engine's
direction-aware, factor-grouped read of *why this signal fired* (TREND / MOMENTUM /
LOCATION & STRENGTH / VOLATILITY / RISK / CONTEXT), with graded `●●●○` marks for
continuous factors and `✓`/`✗` for binaries. It is sourced from the live signal's
`SignalResult.checks` (built only on the live path, `signal(with_checks=True)`), so it
can never drift from the real decision and the backtest replays bit-identically.
With `--allow-shorts`, the stdout summary adds a **SHORTS** block (short
entries) and a **COVERS** block (held-short exits) alongside ENTRIES/EXITS.

Held long positions (from the `positions` table) are also force-exited live when
they reach the max-hold cap (`execution.max_hold_days`, default 25d `if_not_profit`)
— a `time_stop` EXIT — using the same `core.exits.max_hold_exit_due` rule as the
backtester, so live and backtest stay in step.

### `position_CLI.py` — manual positions

```bash
python position_CLI.py {list | open | close | stop} ...
```

| Sub-command | Required args    | Options                                          | Effect                        |
|-------------|------------------|--------------------------------------------------|-------------------------------|
| `list`      | —                | —                                                                   | List all positions.           |
| `open`      | `ticker` `price` | `--side long` (default), `--stop F`, `--date YYYY-MM-DD`, `--notes S` | Insert new position.          |
| `close`     | `id` `price`     | —                                                                   | Close by id.                  |
| `stop`      | `id` `price`     | —                                                                   | Update stop on open position. |

`--date` backfills a **retroactive open** (default: today); the date must be
ISO `YYYY-MM-DD` and not in the future. Use it to log fills after the fact so
the live-vs-backtest reconciliation has real positions to score.

`--side short` is accepted so manual short positions can be tracked. The short
signal/exit pipeline is wired end-to-end and gated behind `main.py
--allow-shorts`.

### `backtest/run_backtest.py` — backtester

```bash
python -m backtest.run_backtest [mode-flags] [window/output flags]
```

Modes (mutually exclusive, choose at most one):

| Flag              | Description                                                 |
|-------------------|-------------------------------------------------------------|
| *(none)*          | Single baseline run.                                        |
| `--sweep`         | Full OFAT parameter grid (`backtest/sweep.py::PARAM_GRID`). |
| `--sweep --quick` | Reduced grid (fewer values per parameter).                  |
| `--mean-rev-tune` | Focused mean-reversion parameter sweep.                     |
| `--scoring-sweep` | settings.yaml scoring weights + thresholds sweep.           |
| `--walk-forward`  | Rolling IS / OOS validation (3y IS / 1y OOS by default).    |
| `--robustness`    | Perturb each parameter ±10/20% and report E[R] sensitivity. |

Window / IO flags:

| Flag                  | Default             | Description                                          |
|-----------------------|---------------------|------------------------------------------------------|
| `--start YYYY-MM-DD`  | None (all data)     | First in-window date.                                |
| `--end YYYY-MM-DD`    | None (all data)     | Last in-window date.                                 |
| `--tickers T [T ...]` | watchlist.yaml      | Restrict universe.                                   |
| `--workers N`         | 1                   | ProcessPool size for sweep / walk-forward.           |
| `--out DIR`           | `data/backtest_out` | Output directory.                                    |
| `--no-html`           | False               | Skip HTML report.                                    |
| `--no-csv`            | False               | Skip CSV ledger.                                     |
| `--journal`           | (default ON)        | Deprecated/no-op — journaling is ON by default. Kept for compatibility. |
| `--no-journal`        | False               | Opt OUT of MySQL journaling for a throwaway run.     |
| `--log LEVEL`         | WARNING             | DEBUG / INFO / WARNING / ERROR.                      |

Strategy opt-in flags (each defaults **OFF** so the baseline replays
bit-identically; turn on to A/B a refinement):

| Flag                | Effect                                                                                                                                                                                                                                                                                                                                                                                              | Config key                             |
|---------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------|
| `--chronic-penalty` | Per-ticker chronic-loser **size penalty**: after repeated losses inside a rolling window, scale that ticker's position size down (sliding scale → 0).                                                                                                                                                                                                                                               | `chronic_loser_penalty` (filters.yaml) |
| `--vix-slope-gate`  | **Block fresh momentum entries when VIX is rising** over the configured lookback window (risk-off filter; mean-reversion entries unaffected).                                                                                                                                                                                                                                                       | `regime.vix_slope_block`               |
| `--anti-gap-entry`  | Require the **trigger bar to close ≥ its open** before queuing the T+1 entry.                                                                                                                                                                                                                                                                                                                       | `signals.require_trigger_bar_up`       |
| `--allow-shorts`    | Enable **short-side entries**: the engine fires shorts in BEAR regimes; the long-only baseline is unchanged when off. Also a `main.py` flag.                                                                                                                                                                                                                                             | `signals.allow_shorts`                 |
| `--max-hold-days N` | **Swing-horizon exit:** force-close a held trade at the bar's **close** once held `N` trading bars (exit reason `time_stop`). Pair with `--max-hold-mode {hard,if-not-profit}` — `hard` always cuts at the cap; `if-not-profit` cuts only when not in profit (lets winners run). **Default `25` bars, `if_not_profit`** (set in `filters.yaml`; it dominates `hard` on every metric — see `ADR-001`). Override or disable via the flags / config. | `execution.max_hold_days` / `execution.max_hold_mode` (filters.yaml) |
| `--max-open-risk R` | **Portfolio open-risk budget** (default `5.0`), in `size_mult` units. Each open position consumes its own `size_mult`, so a new entry is dropped once total open risk would exceed the budget — a half-size (regime/chronic-reduced) position uses half a slot. A risk control, so it is universe-agnostic (not a raw count). Lower → fewer concurrent positions. (`5.0` is the risk-adjusted optimum, re-confirmed 2026-06-05 at the `if_not_profit` config via `scripts/budget_sweep.py`.) | `portfolio.max_open_risk` (`base_port`) |
| `--scoring`         | Enable the `SignalScorer` (`min_score_to_alert` gate + score-ranked budget fill). **OFF by default** — the entry score is non-predictive of R (corr −0.03) and ranking by it selected weaker trades; turning it off lifted the headline Sharpe 0.42 → 0.66 (ADR-003). `--scoring-sweep` forces it on. | `SweepEngine(use_scoring=)` |

> `--journal` requires `config/secrets.env` (`DB_*`). `run_backtest.py`
> loads it at startup, and the `backtest_runs`/`backtest_trades` tables
> from `data/backtest_schema.sql` must exist.

Each flag forces its config key on for that run (the CLI is the explicit
opt-in even if the YAML default is `false`) and prints a `▸ … ENABLED`
line at startup. The same `signals.require_trigger_bar_up` /
`regime.vix_slope_block` / `chronic_loser_penalty` keys can be set in
the YAML instead for `main.py` (live scan).

The CSV ledger (`data/backtest_out/trades.csv`) includes a `direction`
column (`long` / `short`) for per-direction analysis.

### `backtest/validate_shorts.py` — short-side validation

```bash
# Single ledger (runs checks 1, 2, 3, 5, 6):
python -m backtest.validate_shorts data/backtest_out/trades.csv

# Add check 4 (Sharpe/Calmar shorts-on vs off): produce two ledgers, then
# compare. --baseline is OPTIONAL and takes a real path (no angle brackets).
python backtest/run_backtest.py --start 2000-01-01 --no-html --out data/backtest_out/longonly
python backtest/run_backtest.py --start 2000-01-01 --allow-shorts --no-html
python -m backtest.validate_shorts data/backtest_out/trades.csv --baseline data/backtest_out/longonly/trades.csv
```

Postmortem-style acceptance checks on a `--allow-shorts` trade ledger:
count by direction, stop-out R symmetry, win-rate-by-side gap, by-exit
breakdown, and (with `--baseline`) Sharpe/Calmar shorts-on-vs-off. Run
over a window containing real BEAR regimes. Exits non-zero only on a
hard FAIL. The no-concurrent-long+short invariant is covered by
`tests/test_short_portfolio_guard.py`.

### `backtest/repair_parquet.py`

```bash
python -m backtest.repair_parquet [--dry-run]
```

Re-saves every `data/prices/*.parquet` cross-platform (fixes endianness /
architecture mismatches when moving data between machines).

### Reconciliation — is the edge holding live?

Two read-only meters compare live performance to backtest expectancy (the latest
`backtest_runs`, or `--bt-run-id N`); both flag drift beyond `--drift` R/trade
(default 0.15):

```bash
python scripts/reconcile_live.py     # signal fidelity: replay fired signals through cached prices
python scripts/reconcile_fills.py    # real meter: realized R on actual fills in the positions table
```

`reconcile_live.py` replays every fired entry signal forward under the 25-bar
cap — a *delayed backtest*, so it only judges signal fidelity. `reconcile_fills.py`
scores **closed `positions`** (logged via `position_CLI.py`): realized
R = `(exit-entry)/(entry-stop)` (long) using the initial recorded stop as the
risk unit, bucketed by direction and compared to `backtest_trades`. Open
positions are listed as carried risk but not scored; closed positions without a
recorded stop can't be scored (no risk unit).

### Telegram alerts (push)

The daily scan can push fired signals (entries/exits) to Telegram as a chart photo
+ a compact card caption (direction, entry/stop/target, R:R, regime). Entry cards
also carry a `🔎 TREND ✅ · MOM ✅ · LOC ▫️ · …` factor line — a per-group summary of
the same trigger-panel checks rendered on the chart (one source of truth). Set
`TG_BOT_TOKEN` + **numeric** `TG_CHAT_ID` in `config/secrets.env`, then enable in
`config/settings.yaml`:

```yaml
telegram:
  enabled: true          # default false → scan is byte-identical
  alert_types: [long_entry, exit_long, short_entry, exit_short]
  mute: []               # ticker blocklist
```

The push is **fail-open** — a missing token or Telegram outage degrades to a log
line and never affects the scan. Requires `python-telegram-bot`.

### Telegram daemon (interactive)

`telegram_bot.py` is the phase-2 interactive daemon: it long-polls Telegram and
answers the alert/position buttons + owner-only commands. Set `daemon_enabled: true`
in `settings.yaml::telegram` (so the push attaches inline buttons) and run:

```bash
python telegram_bot.py        # owner-only; needs TG_BOT_TOKEN + numeric TG_CHAT_ID
```

Commands: `/positions` `/pos ID` `/recalc [ID|all]` `/open TICKER PRICE [--stop S] [--short]`
`/close ID [PRICE]` `/stop ID PRICE` `/chart TICKER` `/status` `/scan` `/help`. Inline
buttons: **Log opened** (journals a fill via the broker-adapter seam), **Chart** (fresh
render), and per-position **Stop / Close / Recalc / Chart** — `Close` is gated behind a
Yes/No confirm. `/recalc` re-reads the latest bars and runs the engine exit-check
(read-only). Every position mutation goes through `core.execution.adapter` (journal only —
never auto-executes a real trade).

Single-instance: the daemon takes `data/telegram_bot.lock` and exits cleanly if another
poller holds it (Telegram returns **409 Conflict** if two pollers drain `getUpdates` — stop
any other `python telegram_bot.py` first). Deploy at logon with auto-restart:

```bash
powershell -ExecutionPolicy Bypass -File scripts/register_telegram_bot.ps1
```

## Environment variables (`config/secrets.env`)

Loaded by `python-dotenv` at startup.

| Variable         | Required for                         | Notes                                                                                                  |
|------------------|--------------------------------------|--------------------------------------------------------------------------------------------------------|
| `DB_HOST`        | MySQL journaling, `position_CLI.py`  | Default `localhost`.                                                                                   |
| `DB_PORT`        | MySQL journaling, `position_CLI.py`  | Default `3306`.                                                                                        |
| `DB_USER`        | MySQL journaling, `position_CLI.py`  |
| `DB_PASSWORD`    | MySQL journaling, `position_CLI.py`  |                                                                                                        |
| `DB_NAME`        | MySQL journaling, `position_CLI.py`  |                                                                                                        |
| `FRED_API_KEY`   | `settings.yaml::macro.enabled: true` | Free key: <https://fred.stlouisfed.org/docs/api/api_key.html>.                                         |
| `SEC_USER_AGENT` | reserved                             | Not wired — `form4` uses yfinance, not direct EDGAR yet. For the planned Form 4 XML parser (see TODO). |
| `TG_CHAT_ID`     | `settings.yaml::telegram.enabled`    | **Numeric** chat id; used as the owner allowlist.                                                      |
| `TG_BOT_TOKEN`   | `settings.yaml::telegram.enabled`    | Bot token from @BotFather.                                                                             |

## Configuration files

### `config/filters.yaml`

| Block                                                                            | Purpose                                                                                   |
|----------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `price.min_price`                                                                | Hard floor on close.                                                                      |
| `liquidity.min_dollar_volume_20d`                                                | 20-day avg dollar volume floor.                                                           |
| `market_cap.min_market_cap`                                                      | Market cap floor (skipped when cap is None — ETFs / indices).                             |
| `volatility.{min,max}_atr_pct`                                                   | ATR% band.                                                                                |
| `trend.{ma_fast,ma_slow}`                                                        | MA periods (50/200). Used by ticker-trend + regime + scoring.                             |
| `regime.{vix_symbol,vix_low,vix_high}`                                           | Volatility classifier.                                                                    |
| `regime.{index_symbols,require_all_indices,ma_short,require_ma_short_alignment}` | Trend voting + secondary MA-short gate.                                                   |
| `events.{earnings_buffer_days,stop_dates}`                                       | Earnings blackout + manual stop-date calendar.                                            |
| `execution.{entry_slippage_pct,commission_r}`                                    | Backtest fill model.                                                                      |
| `execution.{max_hold_days,max_hold_mode}`                                        | Swing-horizon exit. **Default `25` bars, `if_not_profit`.** `max_hold_days` = bars before a held trade is closed at the bar close (`time_stop`); `max_hold_mode` = `hard` / `if_not_profit` (lets winners run). CLI `--max-hold-days` / `--max-hold-mode` override. |
| `signals.momentum.long`                                                          | Momentum-long entry: rsi band, min_hist_delta_atr, max_bars_since_cross.                  |
| `signals.momentum.short`                                                         | Held-long *momentum-fade exit* (legacy name; canonical at `signals.exits.momentum_fade`). |
| `signals.mean_reversion.long`                                                    | Mean-rev entry: rsi_max, min_hist_delta_atr.                                              |
| `signals.mean_reversion.short`                                                   | Held-long *overbought exit* (legacy; canonical at `signals.exits.mean_rev_overbought`).   |
| `signals.gap_risk.{enabled,max_prev_bar_range_atr}`                              | Block entries after wide-range prev bar.                                                  |
| `signals.sector_gate.{enabled,sector_map_path}`                                  | Block entries when sector ETF below MA.                                                   |
| `signals.exits.{regime_flip,momentum_fade,mean_rev}`                             | Boolean toggles (also accept dict for `signals.exits.*` parameter blocks).                |
| `signals.stop_loss.{atr_multiplier,min_rr}`                                      | Stop distance + R:R sanity.                                                               |
| `signals.stop_loss.min_rr_short`                                                 | Optional: R:R gate for shorts only; absent → falls back to `min_rr`.                      |
| `signals.hard_to_borrow_list`                                                    | Optional: symbols that cannot be shorted (longs unaffected). Default `[]`.                |
| `signals.borrow.{annual_rate_default,per_ticker}`                                | Optional: short stock-borrow cost → per-trade R drag. Default `0.0` (off).                |
| `signals.size_mult_gate.{enabled,min}`                                           | Block entries when composite macro × behavioral size mult < `min`.                        |

### `config/settings.yaml`

| Block                                                                       | Purpose                                                                                                                                                                                                                                                           |
|-----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `storage.{cache_dir,log_level,staleness_hours,staleness_*}`                 | Parquet/JSON cache TTLs + log level.                                                                                                                                                                                                                              |
| `fetcher.max_workers`                                                       | ThreadPool size for watchlist fetch.                                                                                                                                                                                                                              |
| `risk.max_open_risk`                                                        | Aggregate open-risk cap the live scanner surfaces (budget consumed vs. cap, size_mult); alerter only, never auto-executes.                                                                                                                                          |
| `scanner.min_score_to_alert`                                                | Threshold below which a passed signal is `watch_only`.                                                                                                                                                                                                            |
| `scanner.weights`                                                           | Entry sub-score weights (trend_up, ma50_slope, ma200_slope, volume_spike, rsi_healthy, breakout_20d, near_52w_high, far_from_52w_low, macd_bullish, no_earnings_risk, relative_strength, weekly_trend, bb_zscore, rp_percentile, insider_buying, short_interest). |
| `scanner.exit_weights`                                                      | Exit sub-score weights (regime_flip, multi_bar_decay, rsi_overbought, macd_cross_down, vol_expansion, rs_divergence, vbp_resistance).                                                                                                                             |
| `scanner.{entry,exit}_thresholds`                                           | Sub-score tunables (RSI centre/half-width, slope scales, breakout band, VBP distances).                                                                                                                                                                           |
| `scanner.vbp.{lookback,n_bins,volume_percentile}`                           | VBP histogram parameters.                                                                                                                                                                                                                                         |
| `scanner.chart.signal_history`                                              | Render historical signal markers on charts.                                                                                                                                                                                                                       |
| `macro.{enabled,fred_api_key_env,staleness_hours,series_dir,series_subset}` | Macro layer toggles + cache.                                                                                                                                                                                                                                      |
| `macro.{fred_series,boc_series,yf_series}`                                  | Series IDs to fetch.                                                                                                                                                                                                                                              |
| `macro.{size_mult_floor,size_mult_ceiling}`                                 | risk_on_score → size_multiplier mapping.                                                                                                                                                                                                                          |
| `macro.{risk_on_weights,axis_weights}`                                      | Per-axis state→value mapping and weights.                                                                                                                                                                                                                         |
| `behavioral.{enabled,data_dir,stale_window_days}`                           | Behavioral layer toggles + cache.                                                                                                                                                                                                                                 |
| `behavioral.{size_mult_floor,size_mult_ceiling,breadth_divergence_penalty}` | behavioral_score → size_multiplier mapping.                                                                                                                                                                                                                       |
| `behavioral.{behavioral_weights,axis_weights}`                              | Per-axis state→value mapping and weights.                                                                                                                                                                                                                         |

`scanner.weights.insider_buying` and `scanner.weights.short_interest` must be
`0` — the backing fetchers are placeholders; non-zero weight raises
`ConfigError` at scorer construction (see TODO.md).

### `config/watchlist.yaml`

```yaml
tier_a: # scanned + tradeable (~100 tickers)
  - SPY                   # context (regime classifier; must be present)
  - QQQ                   # context
  - ^VIX                  # context-only (not scanned)
  - AAPL
  - ...

tier_b: # RP-rank universe only (not scanned directly)
  - sp500: true           # auto-expand to S&P 500 constituents
  - tsx60: true           # auto-expand to TSX 60 constituents
```

### `config/sector_map.yaml`

Optional ticker → sector ETF map; used when `signals.sector_gate.enabled`.

```yaml
sector_map:
  AAPL: XLK
  XOM: XLE
  # ...
```

Empty `sector_map: { }` is valid (gate becomes a no-op for unmapped tickers).

## Indicators

`core.indicators.indicators.attach_indicators(df, ma_fast=50, ma_slow=200)`
attaches: `atr`, `rsi`, `macd`, `macd_signal`, `macd_hist`, `bb_mid`,
`bb_upper`, `bb_lower`, `bb_bw`, `bb_z`, `ma_fast`, `ma_slow`, `weekly_sma10`.

## Signal pipeline

```
FilterEngine.scan(ticker, df, market_cap)         → ScanResult
FilterEngine.signal(ticker, df, market_dfs, vix_df, earnings_date, held_long, regime)
                                                  → SignalResult
SignalScorer.enrich(signal, df, regime, ...)       (sets was_enriched=True)
```

Direction `long` for fresh entries; `exit_long` for held positions
(`held_long=True`). `SignalResult.size_mult` carries the composite macro ×
behavioral multiplier; backtester scales R-distance by it,
`signals.size_mult_gate` blocks entries below `min`.

## MySQL tables

Credentials from `config/secrets.env`. Each table group ships a DDL file under
`data/` (run once on a fresh deploy, e.g. `mysql -u <user> -p <db> < data/positions_schema.sql`).

| Table             | Module                         | Populated by             | Schema                      |
|-------------------|--------------------------------|--------------------------|-----------------------------|
| `scan_runs`       | `src/persistence/db.py`        | `main.py`                | `data/scan_schema.sql`      |
| `scan_results`    | `src/persistence/db.py`        | `main.py`                | `data/scan_schema.sql`      |
| `backtest_runs`   | `backtest/db.py`               | `run_backtest` (journals by default) | `data/backtest_schema.sql` |
| `backtest_trades` | `backtest/db.py`               | `run_backtest` (journals by default) | `data/backtest_schema.sql` |
| `positions`       | `src/core/position_manager.py` | `position_CLI.py`        | `data/positions_schema.sql` |

## Outstanding work

See `TODO.md`.
