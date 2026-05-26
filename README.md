# TradAlert

Swing-trading scanner and backtesting engine. Detects momentum and mean-reversion entry/exit signals on a configurable
watchlist, scores them with a multi-factor confidence model, and manages positions through a portfolio-aware bar-replay
backtester.

## Architecture

```
config/
  filters.yaml      — scan gates, signal triggers, regime, stop-loss
  settings.yaml     — scoring weights, thresholds, macro & behavioral layers
  watchlist.yaml    — ticker universe (supports tier_a / tier_b structure)

main.py             — live pipeline: fetch → indicators → scan → signal → alert
position_CLI.py     — manual position management (open/close/stop/list)
backtest/
  run_backtest.py   — backtest runner: baseline, OFAT sweep, walk-forward
```

## Quick Start

```bash
# Live scan (uses cached data)
python main.py

# Live scan (force re-fetch all tickers)
python main.py --force

# Backtest baseline
python backtest/run_backtest.py

# Backtest with date window
python backtest/run_backtest.py --start 2022-01-01 --end 2024-12-31

# Full parameter sweep
python backtest/run_backtest.py --sweep --workers 4

# Quick sweep (reduced grid)
python backtest/run_backtest.py --sweep --quick --workers 8
```

## CLI Reference

### `main.py` — Live Scanner Pipeline

Fetches OHLCV, computes indicators, runs the two-stage filter pipeline (scan → signal), enriches with confidence scores,
and outputs alerts.

| Flag      | Default | Description                                           |
|-----------|---------|-------------------------------------------------------|
| `--force` | `False` | Bypass cache staleness check and re-fetch all tickers |

---

### `backtest/run_backtest.py` — Backtest Runner

Bar-replay backtester with portfolio-aware position management. Supports baseline runs, OFAT parameter sweeps,
walk-forward validation, and mean-reversion tuning.

**Modes**

| Command           | Description                                          |
|-------------------|------------------------------------------------------|
| *(no flags)*      | Run baseline config only                             |
| `--sweep`         | Full OFAT parameter sweep (~80 runs)                 |
| `--sweep --quick` | Reduced grid sweep (~60 runs)                        |
| `--mean-rev-tune` | Focused mean-reversion parameter sweep               |
| `--walk-forward`  | Rolling 3yr in-sample / 1yr out-of-sample validation |

**Flags**

| Flag                  | Default             | Description                                                 |
|-----------------------|---------------------|-------------------------------------------------------------|
| `--sweep`             | `False`             | Run full OFAT parameter sweep (~80 runs)                    |
| `--quick`             | `False`             | Reduced grid for sweep (~60 runs)                           |
| `--mean-rev-tune`     | `False`             | Focused mean-reversion parameter sweep                      |
| `--walk-forward`      | `False`             | Rolling 3yr IS / 1yr OOS walk-forward validation            |
| `--start YYYY-MM-DD`  | `None`              | Start date for backtest window                              |
| `--end YYYY-MM-DD`    | `None`              | End date for backtest window                                |
| `--tickers T1 T2 ...` | watchlist           | Override watchlist with specific tickers                    |
| `--earnings-aware`    | `False`             | *(parsed but not currently wired — earnings always loaded)* |
| `--workers N`         | `1`                 | Parallel worker processes (12 is safe on most machines)     |
| `--out DIR`           | `data/backtest_out` | Output directory for reports and CSVs                       |
| `--no-html`           | `False`             | Skip HTML report generation                                 |
| `--no-csv`            | `False`             | Skip CSV export                                             |
| `--journal`           | `False`             | Write baseline run + trades to MySQL (requires DB env vars) |
| `--log LEVEL`         | `WARNING`           | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`              |

**Examples**

```bash
# Baseline on a subset of tickers
python backtest/run_backtest.py --tickers MSFT GOOGL TSLA

# Sweep with 12 workers, from 2020
python backtest/run_backtest.py --sweep --workers 12 --start 2020-01-01

# Walk-forward validation
python backtest/run_backtest.py --walk-forward

# Mean-reversion tuning
python backtest/run_backtest.py --mean-rev-tune

# Full sweep, custom output, MySQL journaling
python backtest/run_backtest.py --sweep --workers 8 --out results/ --journal
```

**Outputs**

| File                                     | Description                                                      |
|------------------------------------------|------------------------------------------------------------------|
| `data/backtest_out/backtest_report.html` | Standalone HTML report with charts, breakdowns, and sweep tables |
| `data/backtest_out/sweep_results.csv`    | Flat CSV with one row per parameter combination                  |
| `data/backtest_out/trades.csv`           | All trades from the baseline run                                 |

---

### `position_CLI.py` — Position Management

Manual position tracking via MySQL. Supports opening, closing, updating stops, and listing positions.

```
python position_CLI.py <command> [args]
```

**Subcommands**

| Command | Positional Args  | Optional Flags                                                            | Description                    |
|---------|------------------|---------------------------------------------------------------------------|--------------------------------|
| `list`  | *(none)*         | *(none)*                                                                  | List all open positions        |
| `open`  | `ticker` `price` | `--side long\|short` (default: `long`), `--stop <float>`, `--notes <str>` | Open a new position            |
| `close` | `id` `price`     | *(none)*                                                                  | Close a position by ID         |
| `stop`  | `id` `price`     | *(none)*                                                                  | Update stop-loss on a position |

**Examples**

```bash
# List all positions
python position_CLI.py list

# Open a long position
python position_CLI.py open NVDA 140.50 --stop 132.00 --notes "momentum breakout"

# Open a short position
python position_CLI.py open TSLA 250.00 --side short

# Close position #5 at $155
python position_CLI.py close 5 155.00

# Update stop on position #3
python position_CLI.py stop 3 135.00
```

---

### `backtest/repair_parquet.py` — Parquet Cache Repair

Re-saves parquet cache files for cross-platform compatibility (fixes endianness / architecture mismatches when moving
data between machines).

| Flag        | Default | Description                                                |
|-------------|---------|------------------------------------------------------------|
| `--dry-run` | `False` | Report files that would be modified without making changes |

```bash
# Check what needs repair
python backtest/repair_parquet.py --dry-run

# Repair all parquet files
python backtest/repair_parquet.py
```

---

## Environment Variables

Loaded from `config/secrets.env` (via `python-dotenv`):

| Variable       | Required For                 | Description                                              |
|----------------|------------------------------|----------------------------------------------------------|
| `DB_HOST`      | Position CLI, SQL journaling | MySQL host (default: `localhost`)                        |
| `DB_PORT`      | Position CLI, SQL journaling | MySQL port (default: `3306`)                             |
| `DB_USER`      | Position CLI, SQL journaling | MySQL username                                           |
| `DB_PASSWORD`  | Position CLI, SQL journaling | MySQL password                                           |
| `DB_NAME`      | Position CLI, SQL journaling | MySQL database name                                      |
| `FRED_API_KEY` | Macro data layer             | FRED API key for interest rate, inflation, credit series |
| `TG_CHAT_ID`   | *(reserved)*                 | Telegram chat ID for alerts (not yet wired)              |
| `TG_BOT_TOKEN` | *(reserved)*                 | Telegram bot token for alerts (not yet wired)            |

## Configuration

### `config/filters.yaml`

Controls the two-stage filter engine:

- **Scan filters**: price floor, dollar-volume, market cap, ATR% band
- **Trend**: MA(50)/MA(200) stack for ticker trend classification
- **Regime**: VIX thresholds, index symbols for broad-market trend
- **Signals**: momentum/mean-reversion entry triggers, exit toggles, stop-loss structure
- **Events**: earnings buffer days, stop-date blackouts
- **Execution**: entry slippage, commission in R units
- **Behavioral**: size multiplier floor for macro/behavioral gating

### `config/settings.yaml`

Controls scoring and data layers:

- **Scanner weights**: entry and exit sub-score weights (trend, RSI, MACD, volume, 52w proximity, RP percentile, VBP,
  etc.)
- **Thresholds**: RSI bands, slope scales, breakout bands, VBP distances
- **Alert gate**: `min_score_to_alert` — signals below this score are `watch_only`
- **Macro**: FRED/BoC/yfinance series, risk-on state mappings, axis weights
- **Behavioral**: COT, NAAIM, AAII, breadth, sentiment state mappings

### `config/watchlist.yaml`

Ticker universe. Supports flat list or two-tier structure:

```yaml
# Flat
tickers: [ AAPL, MSFT, GOOGL ]

# Two-tier
tier_a: [ AAPL, MSFT, GOOGL ]
tier_b: [ AMD, NVDA, TSLA ]
```

Context symbols (`SPY`, `QQQ`, `^VIX`) are always included for regime calculation.

## Backtest Sweep Parameters

--TODO: UPDATE--

## MySQL Journaling

All 5 tables live in the `tradalert` database. Credentials from `config/secrets.env`.

### Always-On: Scan Journaling

Every `python main.py` run automatically writes to these tables:

| Table          | Columns                                                                                                                                                                  | Purpose                                                       |
|----------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------|
| `scan_runs`    | `id`, `forced`, `tickers_attempted`, `tickers_fetched`, `tickers_scanned`, `scan_passed`, `signals_fired`, `market_regime`, `notes`                                      | One row per scan run — summary stats + regime                 |
| `scan_results` | `run_id` (FK), `ticker`, `passed`, `signal_kind`, `score`, `reason`, `close`, `atr`, `atr_pct`, `dv20`, `market_cap`, `rsi`, `macd`, `macd_signal`, `macd_hist`, `error` | One row per ticker — full indicator snapshot + signal outcome |

### On-Demand: Backtest Journaling

Triggered with `--journal` flag on `backtest/run_backtest.py`:

| Table             | Columns                                                                                                                                                                                                                                 | Purpose                                                           |
|-------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| `backtest_runs`   | `id`, `start_date`, `end_date`, `tickers_count`, `trades_count`, `total_r`, `expectancy_r`, `profit_factor`, `win_rate`, `max_drawdown_r`, `config_json`, `notes`                                                                       | One row per backtest run — aggregate stats + full config snapshot |
| `backtest_trades` | `run_id` (FK), `ticker`, `signal_type`, `direction`, `entry_date`, `entry_price`, `initial_stop`, `initial_target`, `exit_date`, `exit_price`, `exit_reason`, `bars_held`, `r_multiple`, `market_regime`, `ticker_trend`, `entry_score` | Individual trades — full trade lifecycle with R multiples         |

### Manual: Position Tracking

Managed via `position_CLI.py`:

| Table       | Columns                                                                                               | Purpose                                                        |
|-------------|-------------------------------------------------------------------------------------------------------|----------------------------------------------------------------|
| `positions` | `id`, `ticker`, `side`, `entry_price`, `entry_date`, `stop_price`, `exit_price`, `exit_date`, `notes` | Manual position CRUD — open positions have `exit_date IS NULL` |

**Schema**: Table structures are defined inline in the Python modules (`src/persistence/db.py`, `backtest/db.py`,
`src/core/position_manager.py`). Create tables manually before first use.
