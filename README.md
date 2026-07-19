# TradAlert

> Swing-trading scanner, signal engine, and 25-year backtester — **one engine,
> replayed bit-for-bit** between the live scan and historical sweeps.

TradAlert scans a watchlist for momentum and mean-reversion swing entries, sizes them
with macro/behavioral regime context, manages held positions with disciplined exits,
and journals everything to MySQL and Telegram. The **same** `FilterEngine` drives both
the live scan and the backtester, so a live signal and its historical replay are
byte-identical — the backtest is the ground truth for every live decision.

**Highlights**

- 📈 **One engine, two modes** — `main.py` (live scan) and `backtest/run_backtest.py`
  (baseline · OFAT sweep · walk-forward · robustness) share `core.FilterEngine`; live ≡ backtest by construction.
- 🧭 **Regime-aware sizing** — macro (FRED/BoC) × behavioral (COT / breadth / sector-rotation)
  composite scales position *size*, never the signal direction.
- 🛡️ **Risk discipline** — ATR stops, R:R gate, max-hold time-stop, breakeven stop (ADR-004),
  and a portfolio open-risk budget.
- 📲 **Telegram cockpit** — push alerts (chart + trigger panel) plus an interactive,
  owner-only daemon that is **journal-only and never auto-trades**.
- 🔬 **Honest validation** — paired same-snapshot A/Bs, walk-forward, and reconciliation
  meters that compare real live fills against backtest expectancy.
- 🖥️ **Local control panel** — a FastAPI + React dashboard (`python -m api --open`) for the
  scanner, backtests, charts, positions and live config; read-only by default, mutations
  gated behind an optional API token, loopback-only unless a token is set.

## Contents

[Layout](#layout) · [Cold start](#cold-start) · [Entry points](#entry-points) ·
[Web control panel](#web-control-panel) ·
[Environment variables](#environment-variables-configsecretsenv) ·
[Configuration files](#configuration-files) · [Indicators](#indicators) ·
[Signal pipeline](#signal-pipeline) · [MySQL tables](#mysql-tables) · [Outstanding work](#outstanding-work)

## Layout

```
main.py                Live scan: fetch → indicators → scan → signal
position_CLI.py        Manual position CRUD
telegram_bot.py        Interactive Telegram daemon (owner-only commands + buttons)
backtest/run_backtest  Backtester: baseline, sweep, walk-forward, robustness
backtest/repair_parquet Cross-platform parquet re-save utility

api/                   FastAPI control-panel backend (read endpoints + job launchers)
frontend/              Vite/React SPA (built to web/dist, served by api at "/")
web/                   Single-file fallback panel (index.html) + built SPA (dist/)
scripts/               Live ops + studies: intraday_monitor, reconcilers, task registrars, A/B harnesses

config/
  filters.yaml         Scan + signal + regime + stop-loss + exit rules
  settings.yaml        Macro/behavioral layers, risk budget, telegram, storage
  watchlist.yaml       Two-tier ticker universe (tier_a tradeable, tier_b RP-only)
  sector_map.yaml      Optional ticker → sector ETF mapping for sector_gate
  secrets.env          Local secrets (gitignored; see secrets.env.example)

src/core/              Domain: filter_engine, regime (MarketRegime + classifier), ticker_store, types, paths, defaults
src/core/fetchers/     yfinance OHLCV, FRED/BoC macro, behavioral (COT/breadth/sector-rotation)
src/core/indicators/   ATR/RSI/MACD/Bollinger + VBP + chart renderer
src/core/macro/        Macro regime classifier (risk_on_score, size_multiplier)
src/core/behavioral/   Behavioral regime classifier (breadth, sector-rotation, positioning)
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
python main.py [--force] [--allow-shorts]
```

| Flag             | Default | Description                                                                                                                                          |
|------------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--force`        | False   | Bypass cache staleness check; re-fetch every ticker.                                                                                                 |
| `--allow-shorts` | False   | Enable short-side entries. Sets `signals.allow_shorts=true` in the loaded filters config. Off keeps the long-only baseline replay-stable. |

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

**Data-freshness tier + event-risk (live-only).** Before each ticker the scan drops any
unclosed current-day bar and force-refetches stale data; a fired entry that is still
stale-after-refetch, gapped > 2×ATR overnight, or whose live gap can't be verified is
downgraded from `LIVE` to **NEEDS_REVIEW** — split into a separate `⚠ NEEDS REVIEW`
stdout block (with the reason) and persisted to `scan_results.tier`/`review_reason`.
When a fresh entry lands within `scanner.event_risk_within_days` (default 5) of a
scheduled FOMC/CPI/NFP, the summary also prints a display-only `⚠ EVENT RISK` advisory
(never gates or sizes). Its calendar resolves a YAML override → the live TradingView
economic-calendar feed (cached, fail-open, forward-extending) → a hard-coded fallback
list, so the advisory survives without manual upkeep. Both are LIVE-path only, so the
backtest stays byte-identical.

Held positions (long or short) are also force-exited live when they reach the
max-hold cap (`execution.max_hold_days`, default 25d `if_not_profit`) — a `time_stop`
EXIT (a COVER for a held short) — using the same `core.exits.max_hold_exit_due` rule
as the backtester, so live and backtest stay in step. Likewise, once a held position's
best excursion reaches `execution.breakeven_trigger_r` (default `1.0`, ADR-004) the
scan raises `positions.stop_price` to breakeven via the shared
`core.exits.breakeven_stop_level` rule (a Telegram notice is sent; `initial_stop`
is never touched, so realized-R reconciliation is unaffected).

### `scripts/live/intraday_monitor.py` — 1h held-long breakdown monitor (live-only)

```bash
python scripts/live/intraday_monitor.py [--force] [--dry-run]
```

A midday heads-up between EOD scans: for each **open long**, it fetches 1h bars and
Telegram-alerts when the last **completed** 1h bar closes below the position's stop
(the still-forming hour is excluded, so a partial bar can't false-trigger). Shorts are
excluded; it is **journal/alert only and never places an order**. One alert per
breakdown episode, re-armed once price recovers to/above the stop (dedup state in
`data/intraday_monitor_state.json`). `--force` runs outside market hours; `--dry-run`
logs instead of sending. Live-only — the backtester never imports it. Run hourly at
logon via `powershell -ExecutionPolicy Bypass -File scripts/setup/register_intraday_monitor.ps1`.

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

Strategy refinement flags — each **CLI flag** defaults off, but the YAML supplies the
operative default: the shipped `filters.yaml` already enables max-hold
(`25`/`if_not_profit`), the breakeven stop (`1.0R`, ADR-004) and the anti-gap trigger
filter, so the no-flag baseline runs **with** those (it is the shipped headline config, not
a bare strategy). Pass a flag to force its key on for an A/B even when the YAML default is off:

| Flag                | Effect                                                                                                                                                                                                                                                                                                                                                                                              | Config key                             |
|---------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------|
| `--chronic-penalty` | Per-ticker chronic-loser **size penalty**: after repeated losses inside a rolling window, scale that ticker's position size down (sliding scale → 0).                                                                                                                                                                                                                                               | `chronic_loser_penalty` (filters.yaml) |
| `--vix-slope-gate`  | **Block fresh momentum entries when VIX is rising** over the configured lookback window (risk-off filter; mean-reversion entries unaffected).                                                                                                                                                                                                                                                       | `regime.vix_slope_block`               |
| `--anti-gap-entry`  | Require the **trigger bar to close ≥ its open** before queuing the T+1 entry.                                                                                                                                                                                                                                                                                                                       | `signals.require_trigger_bar_up`       |
| `--allow-shorts`    | Enable **short-side entries**: the engine fires shorts in BEAR regimes; the long-only baseline is unchanged when off. Also a `main.py` flag.                                                                                                                                                                                                                                             | `signals.allow_shorts`                 |
| `--max-hold-days N` | **Swing-horizon exit:** force-close a held trade at the bar's **close** once held `N` trading bars (exit reason `time_stop`). Pair with `--max-hold-mode {hard,if-not-profit}` — `hard` always cuts at the cap; `if-not-profit` cuts only when not in profit (lets winners run). **Default `25` bars, `if_not_profit`** (set in `filters.yaml`; it dominates `hard` on every metric — see `ADR-001`). Override or disable via the flags / config. | `execution.max_hold_days` / `execution.max_hold_mode` (filters.yaml) |
| `--breakeven-trigger-r R` | **Breakeven stop:** once a held trade's best excursion reaches `R` (in initial-risk units), the stop moves to entry — protects the give-back leak without capping the upside (it does not trail further, so winners still run to target). **Default `1.0`** (set in `filters.yaml`; validated walk-forward-stable with better totals, Sharpe and drawdown — see `ADR-004`). Pass `0` to disable. The R denominator stays the **initial** stop. The live scan applies the same rule to held positions (raises `positions.stop_price`, never `initial_stop`). | `execution.breakeven_trigger_r` (filters.yaml) |
| `--max-open-risk R` | **Portfolio open-risk budget** (default `5.0`), in `size_mult` units. Each open position consumes its own `size_mult`, so a new entry is dropped once total open risk would exceed the budget — a half-size (regime/chronic-reduced) position uses half a slot. A risk control, so it is universe-agnostic (not a raw count). Lower → fewer concurrent positions. (`5.0` is the risk-adjusted optimum, re-confirmed 2026-06-05 at the `if_not_profit` config via `scripts/studies/budget_sweep.py`.) | `portfolio.max_open_risk` (`base_port`) |
| `--correlation-cap` | **Correlation-aware open-risk budget** (default **OFF**): charge `--max-open-risk` against the correlation-adjusted effective risk `√(wᵀCw)` rather than the raw `size_mult` sum, so correlated concurrent names share a budget slot. Tune with `--correlation-lookback N` (return window in bars, default 60), `--correlation-min-overlap N` (min overlapping days to trust a pair, default 40), `--correlation-floor F` (correlations below `F` — and all negatives — count as 0). Effective ≤ raw for ρ∈[0,1], so it is monotone-safe. A/B'd via `scripts/studies/paired_ab_correlation.py`: at the shipped 5.0R budget it *hurts* the North Star (Sharpe 0.66→0.60), so it ships off — kept as a documented, tested lever. | `portfolio.correlation_*` (`base_port`) |

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
scoring-OFF `backtest_runs`, matching the live default, or `--bt-run-id N`); both flag
drift beyond `--drift` R/trade (default 0.15):

```bash
python scripts/live/reconcile_live.py     # signal fidelity: replay fired signals through cached prices
python scripts/live/reconcile_fills.py    # real meter: realized R on actual fills in the positions table
```

`reconcile_live.py` replays every fired **LIVE-tier** entry signal forward under the
25-bar cap — a *delayed backtest*, so it only judges signal fidelity; NEEDS_REVIEW
(stale/gapped) fires are held out, not scored. `reconcile_fills.py`
scores **closed `positions`** (logged via `position_CLI.py`): realized
R = `(exit-entry)/(entry-stop)` (long) using the initial recorded stop as the
risk unit, bucketed by direction and compared to `backtest_trades.r_multiple`
— the same per-unit convention, gross of borrow and unscaled by size_mult,
so the comparison isolates entry/exit quality from the sizing layer. Open
positions are listed as carried risk but not scored; closed positions without a
recorded stop can't be scored (no risk unit).

### Telegram alerts (push)

The daily scan can push fired signals (entries/exits) to Telegram as a chart photo
+ a compact card caption (direction, entry/stop/target, R:R, regime). Entry cards
also carry a `🔎 TREND ❌ · MOM ✅ · LOC ▫️ · …` factor line — a per-group summary of
the same trigger-panel checks rendered on the chart (one source of truth). Set
`TG_BOT_TOKEN` + **numeric** `TG_CHAT_ID` in `config/secrets.env`, then enable in
`config/settings.yaml`:

```yaml
telegram:
  enabled: true          # default false → scan is byte-identical
  alert_types: [long_entry, exit_long, short_entry, exit_short]
  mute: []               # ticker blocklist
  regime_flip_exit_mode: advisory   # advisory | exit | off
```

The push is **fail-open** — a missing token or Telegram outage degrades to a log
line and never affects the scan. Requires `python-telegram-bot`.

**Regime-flip exits are consolidated, not a flatten-all.** A broad regime flip
fires an `exit_long` on *every* held long at once, which as individual cards reads
like a directive to close the whole book on the first CHOP bar. `regime_flip_exit_mode`
(default `advisory`) collapses those into **one `⚠️ REGIME CAUTION` message** that lists
the affected positions and states plainly that nothing is auto-closed — the trader trims
or holds at their discretion. Position-specific exits (momentum-fade, mean-reversion) are
untouched and still fire their own EXIT cards. Set `exit` for the legacy per-position
cards, or `off` to suppress. This is a live-push-only knob (the engine/backtester never
see it, so the backtest stays byte-identical).

### Telegram daemon (interactive)

`telegram_bot.py` is an interactive, **owner-only** daemon: it long-polls Telegram
and answers the alert/position buttons plus typed commands. Set `daemon_enabled: true`
in `settings.yaml::telegram` (so the push attaches inline buttons) and run:

```bash
python telegram_bot.py        # owner-only; needs TG_BOT_TOKEN + numeric TG_CHAT_ID
```

**Commands:** `/positions` (held-position dashboard) · `/pos ID` · `/recalc [ID|all]` ·
`/open TICKER PRICE [--stop S] [--short]` · `/close ID [PRICE]` · `/stop ID PRICE` ·
`/edit ID FIELD VALUE` · `/alert TICKER above|below PRICE` · `/alerts` (list; `/alert del ID`) ·
`/chart TICKER` · `/status` · `/scan` · `/help`.

**Inline buttons:**

- **Entry card** — `📈 Log opened` → a fill picker (`💹 @ live` real quote · `🏷 @ ref`
  alert price · `✍️ Custom` typed), `📊 Chart` (fresh render), `🚫 Skip` (journals a
  pass-on for the opportunity tracker).
- **Open position** — `🟰 Breakeven` / `🔒 +1R` one-tap stop moves, `✏️ Stop` (typed),
  `➖ Close…` (`½` / `⅓` / `⬛ Full`), `🔄 Recalc` (read-only exit-check), `✏️ Edit`, `📈 Chart`.
- **Closed position** — `✏️ Edit` (fix a mis-logged fill) · `📈 Chart` (realized-R card).
- Destructive actions (a full close) are gated behind a `✅ Yes` / `✖ No` confirm.

Every position mutation goes through `core.execution.adapter` — **journal only; it never
auto-executes a real trade** (the owner places each fill manually).

**Price alerts.** `/alert TICKER above|below PRICE` arms an owner-set target crossing;
`/alerts` lists the active ones and `/alert del ID` removes one. A 5-minute in-daemon
poller checks the latest cached prices and pushes a one-shot notice when a target is
crossed — journal/alert only, never an order. Backed by the `price_alerts` table
(`data/price_alerts_schema.sql`, applied once; restart the daemon after applying).

Single-instance: the daemon takes `data/telegram_bot.lock` and exits cleanly if another
poller holds it (Telegram returns **409 Conflict** if two pollers drain `getUpdates` — stop
any other `python telegram_bot.py` first). Deploy at logon with auto-restart:

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup/register_telegram_bot.ps1
```

### AI advisor (live-only second opinion)

Gated by `settings.yaml::advisor.enabled`. A **hybrid** critic on every fired entry:
a deterministic Python rubric computes the verdict and a calibrated confidence from
technical posture plus historical base rates (`data/advisor_base_rates.json`,
rebuilt per journaled backtest via `scripts/studies/build_advisor_base_rates.py
--latest`), while a local LLM (Ollama; `qwen3:8b` default) reads **only the news**
— it can add caution (downgrade / veto on adverse catalysts, penalize when blind)
but can never inflate a weak setup, so a negative-EV signal cannot be rubber-stamped.

Ticker news is gathered multi-source (Google News on the company name, Finnhub,
AlphaVantage, Yahoo/Brave backstops), relevance-filtered with price-recaps demoted,
and **SEC EDGAR 8-K material events** (keyless) are prepended as the highest-signal
issuer-authored items (`news.sec_filings`). The verdict + conviction render on the
Telegram/web cards and journal to `scan_results.advisor_note`;
`scripts/live/evaluate_advisor.py` scores resolved verdicts once enough accumulate.

Live-path only — the advisor is never imported by the engine or backtester, so every
backtest replays byte-identically with it on or off. `scripts/setup/ensure_ollama.ps1`
(hooked into `scripts/run_daily.bat`) starts Ollama before the scheduled scan so the
news read doesn't silently degrade.

## Web control panel

A local dashboard over the same engine and journals — scanner results, backtest launch +
history, price/indicator charts, held-position editing, and a live config view. The backend
is a read-only-by-default FastAPI app (`api/`); the UI is a Vite/React SPA (`frontend/`) with
a no-build single-file fallback (`web/index.html`).

```bash
# Backend — serves the built SPA at "/", falls back to the single-file panel:
python -m api --open                 # http://localhost:8000, opens a browser
# or: uvicorn api.main:app --port 8000

# Build the SPA (outputs to web/dist, which the API mounts at "/"):
cd frontend && npm install && npm run build
# Dev SPA with hot-reload (Vite :5173, CORS-allowed against the API):
npm run dev
```

Until the SPA is built, `/` serves the single-file panel; it is always reachable at `/legacy`.

**Endpoints** (under `/api`): *read* — `/health`, `/scanner/latest`, `/scanner/runs`,
`/positions`, `/config`, `/charts/{ticker}`, `/backtests`,
`/backtests/{run_id}/{equity,monthly,trades}`, `/backtests/jobs/{id}[/stream]`; *mutating* —
`POST /scan`, `/backtests/run`, `/positions`, `/positions/{id}/{close,scale-out}`,
`POST /config`, `PATCH /positions/{id}[/stop]`. Backtest and scan runs launch the real
scripts as streamable background jobs; position edits and config writes are journal-only,
exactly like the CLI/Telegram paths.

**Safety.** Mutating routes are open for single-operator localhost use, but require an
`X-API-Token` header once `TRADALERT_API_TOKEN` is set (compared in constant time).
`python -m api` **refuses a non-loopback `--host`** unless that token is configured, so a
stray bind can't expose the control surface to the LAN.

## Environment variables (`config/secrets.env`)

Loaded by `python-dotenv` at startup.

| Variable         | Required for                         | Notes                                                                                                  |
|------------------|--------------------------------------|--------------------------------------------------------------------------------------------------------|
| `DB_HOST`        | MySQL journaling, `position_CLI.py`  | Default `localhost`.                                                                                   |
| `DB_PORT`        | MySQL journaling, `position_CLI.py`  | Default `3306`.                                                                                        |
| `DB_USER`        | MySQL journaling, `position_CLI.py`  |                                                                                                        |
| `DB_PASSWORD`    | MySQL journaling, `position_CLI.py`  |                                                                                                        |
| `DB_NAME`        | MySQL journaling, `position_CLI.py`  |                                                                                                        |
| `FRED_API_KEY`   | `settings.yaml::macro.enabled: true` | Free key: <https://fred.stlouisfed.org/docs/api/api_key.html>.                                         |
| `SEC_USER_AGENT` | reserved                             | Not yet read — the EDGAR Form-4 fetcher (`scripts/studies/form4_fetch.py`) hardcodes its contact UA; documented in `secrets.env.example` but unconsumed.                                                |
| `TG_CHAT_ID`     | `settings.yaml::telegram.enabled`    | **Numeric** chat id; used as the owner allowlist.                                                      |
| `TG_BOT_TOKEN`   | `settings.yaml::telegram.enabled`    | Bot token from @BotFather.                                                                             |
| `TRADALERT_API_TOKEN` | Web control panel (optional)    | When set, the `api/` backend requires it as an `X-API-Token` header on every mutating route and lets `python -m api` bind a non-loopback `--host`. Unset → mutations open, loopback only. |

## Configuration files

### `config/filters.yaml`

| Block                                                                            | Purpose                                                                                   |
|----------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `price.min_price`                                                                | Hard floor on close.                                                                      |
| `liquidity.min_dollar_volume_20d`                                                | 20-day avg dollar volume floor.                                                           |
| `market_cap.min_market_cap`                                                      | Market cap floor (skipped when cap is None — ETFs / indices).                             |
| `volatility.{min,max}_atr_pct`                                                   | ATR% band.                                                                                |
| `trend.{ma_fast,ma_slow}`                                                        | MA periods (50/200). Used by ticker-trend + regime.                                       |
| `regime.{vix_symbol,vix_low,vix_high}`                                           | Volatility classifier.                                                                    |
| `regime.{index_symbols,require_all_indices,ma_short,require_ma_short_alignment}` | Trend voting + secondary MA-short gate.                                                   |
| `events.{earnings_buffer_days,stop_dates}`                                       | Earnings blackout + manual stop-date calendar.                                            |
| `execution.{entry_slippage_pct,commission_r}`                                    | Backtest fill model.                                                                      |
| `execution.{max_hold_days,max_hold_mode}`                                        | Swing-horizon exit. **Default `25` bars, `if_not_profit`.** `max_hold_days` = bars before a held trade is closed at the bar close (`time_stop`); `max_hold_mode` = `hard` / `if_not_profit` (lets winners run). CLI `--max-hold-days` / `--max-hold-mode` override. |
| `execution.breakeven_trigger_r`                                                  | Breakeven stop trigger in initial-risk units. **Default `1.0`** (ADR-004): at `+1R` best excursion the stop moves to entry, upside uncapped; applied identically in the backtest and the live scan. `0`/absent = off; CLI `--breakeven-trigger-r` overrides. |
| `signals.momentum.long`                                                          | Momentum-long entry: rsi band, min_hist_delta_atr, max_bars_since_cross.                  |
| `signals.momentum.short`                                                         | Held-long *momentum-fade exit* (legacy name; canonical at `signals.exits.momentum_fade`). |
| `signals.mean_reversion.long`                                                    | Mean-rev entry: rsi_max, min_hist_delta_atr.                                              |
| `signals.mean_reversion.short`                                                   | Held-long *overbought exit* (legacy; canonical at `signals.exits.mean_rev`).   |
| `signals.gap_risk.{enabled,max_prev_bar_range_atr}`                              | Block entries after wide-range prev bar.                                                  |
| `signals.sector_gate.{enabled,sector_map_path}`                                  | Block entries when sector ETF below MA.                                                   |
| `signals.exits.{regime_flip,momentum_fade,mean_rev}`                             | Held-long exit toggles (also accept dict for `signals.exits.*` parameter blocks).         |
| `signals.exits.{regime_flip_bear_only,regime_flip_confirm_bars}`                 | Regime-flip exit shaping (A/B levers). `regime_flip_bear_only` (default `false`) exits only on BEAR (CHOP no longer flattens); `regime_flip_confirm_bars` (default `1`) requires the flip to persist N bars first. Defaults reproduce the exit-on-any-non-BULL-bar behavior byte-for-byte. **A/B + walk-forward (`scripts/studies/regime_exit_{ab,wf}.py`, snapshot 2026-06-10):** full-period `bear_only` looks strong (+31.97R, +0.093 Sharpe, −7.89R maxDD) but the walk-forward **refutes it as a default** — it wins totalR in only 10/26 yearly windows, the aggregate rides ~5 years (2004/12/13/14/17), and it nets negative over the last 5. Kept as a documented, tested lever (ships `false`); `confirm_bars` is marginal-to-negative. The live "flatten-on-chop" UX concern is handled separately by `telegram.regime_flip_exit_mode`. |
| `signals.stop_loss.{atr_multiplier,min_rr}`                                      | Stop distance + R:R sanity.                                                               |
| `signals.stop_loss.min_rr_short`                                                 | Optional: R:R gate for shorts only; absent → falls back to `min_rr`.                      |
| `signals.hard_to_borrow_list`                                                    | Optional: symbols that cannot be shorted (longs unaffected). Default `[]`.                |
| `signals.borrow.{annual_rate_default,per_ticker}`                                | Optional: short stock-borrow cost → per-trade R drag. Default `0.0` (off).                |

### `config/settings.yaml`

| Block                                                                       | Purpose                                                                                                                                                                                                                                                           |
|-----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `storage.{cache_dir,log_level,staleness_hours,staleness_*}`                 | Parquet/JSON cache TTLs + log level.                                                                                                                                                                                                                              |
| `fetcher.max_workers`                                                       | ThreadPool size for watchlist fetch.                                                                                                                                                                                                                              |
| `risk.max_open_risk`                                                        | Aggregate open-risk cap the live scanner surfaces (budget consumed vs. cap, size_mult); alerter only, never auto-executes.                                                                                                                                          |
| `scanner.chart.signal_history`                                              | Render historical signal markers on charts.                                                                                                                                                                                                                       |
| `scanner.event_risk_within_days`                                           | Advisory window (calendar days, default 5): surface an upcoming FOMC/CPI/NFP on a fresh entry; display-only, never gates/sizes (distinct from the `events.stop_dates` entry-day block). |
| `macro.{enabled,fred_api_key_env,staleness_hours,series_dir,series_subset}` | Macro layer toggles + cache.                                                                                                                                                                                                                                      |
| `macro.{fred_series,boc_series,yf_series}`                                  | Series IDs to fetch.                                                                                                                                                                                                                                              |
| `macro.{size_mult_floor,size_mult_ceiling}`                                 | risk_on_score → size_multiplier mapping.                                                                                                                                                                                                                          |
| `macro.{risk_on_weights,axis_weights}`                                      | Per-axis state→value mapping and weights.                                                                                                                                                                                                                         |
| `behavioral.{enabled,data_dir,stale_window_days}`                           | Behavioral layer toggles + cache.                                                                                                                                                                                                                                 |
| `behavioral.{size_mult_floor,size_mult_ceiling,breadth_divergence_penalty}` | behavioral_score → size_multiplier mapping.                                                                                                                                                                                                                       |
| `behavioral.{behavioral_weights,axis_weights}`                              | Per-axis state→value mapping and weights.                                                                                                                                                                                                                         |

### `config/watchlist.yaml`

```yaml
tier_a: # scanned + tradeable (~225 names; SPY/QQQ/^VIX are context)
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
FilterEngine.signal(ticker, df, market_dfs, vix_df, earnings_date, held_long, held_short, regime, with_checks)
                                                  → SignalResult
```

Direction `long`/`short` for fresh entries; `exit_long`/`exit_short` for held
positions (`held_long`/`held_short=True`). `SignalResult.size_mult` carries the composite macro ×
behavioral multiplier; the backtester scales R-distance by it (sizing layer, never the
direction).

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
| `price_alerts`    | `src/persistence/db.py`        | `telegram_bot.py` (`/alert`) | `data/price_alerts_schema.sql` |

## Outstanding work

See `TODO.md`.
