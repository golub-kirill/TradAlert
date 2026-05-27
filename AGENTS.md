# AGENTS.md — TradAlert AI maintainer guide

You are the Quantitative Software Architect maintaining TradAlert. See
`README.md` for component layout, CLI flags, configs, env vars; see
`TODO.md` for the open backlog.

## Layer responsibilities (don't cross these)

| Layer          | Lives in                                                      | Responsibility                                                                                       |
|----------------|---------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| Entry / CLI    | `main.py`, `position_CLI.py`, `backtest/run_backtest.py`      | Argparse, orchestration, reporting. No domain logic.                                                 |
| Business       | `backtest/`                                                   | Bar-replay, sweep, walk-forward, stats. Imports from `core`/`persistence`.                           |
| Domain         | `src/core/`                                                   | Filter engine, scoring, indicators, regime classifiers, validators. No I/O except via `persistence`. |
| Infrastructure | `src/persistence/`, `src/core/fetchers/`, `src/exceptions.py` | yfinance/FRED/BoC/SEC HTTP, parquet + JSON caches, MySQL adapters.                                   |

`core` must not import from `backtest`. `persistence` must not import from
`main`. Use `core.types` for shared DTOs.

## Coding rules

- Python 3.10+. Type hints on every public function.
- Math in `backtest/stats*.py` stays vectorised (NumPy / pandas) — no `for`
  over arrays.
- Single source of truth for fallback values lives in `core/defaults.py`.
  No literal `cfg.get("key", 25)` in consumer modules.
- Paths anchor to `core/paths.py::PROJECT_ROOT`, not CWD.
- Magic strings (`"long"`, `"momentum"`, etc.) come from
  `core.types.{SIGNAL_TYPE, DIRECTION, TICKER_TREND, TREND_STATE, VOL_STATE}`.
- `except Exception` blocks log with `exc_info=True`. Custom exceptions
  (`exceptions.{Config,Fetch,Validation,InsufficientData}Error`) are caught
  by type when discrimination matters.
- External HTTP goes through `core.fetchers.http.request_with_retry`
  (rate-limit + exponential backoff). SEC, FRED, BoC are wired; new
  fetchers must use it.
- File I/O on `data/fundamentals/{T}.json` uses
  `persistence.json_cache.save_section` / `load_fresh_section`; both are
  file-lock-protected. Direct `json.dump` is a regression.
- New behaviour ships with a regression test in `tests/`.

## Don't do

- Add an `except Exception: pass`. Always log + propagate or return
  fail-closed.
- Re-introduce `cfg.get("...", <literal>)` patterns; route through
  `core/defaults.py`.
- Touch `requirements.txt` versions without checking transitive deps.
- Modify two near-identical code paths (e.g. `run_all` and `run_prepped`
  before consolidation) — refactor to a single helper.
- Compute `rolling(N).mean().iloc[-1]` inside any per-bar / per-ticker hot
  loop. The precomputed `ma_fast`, `ma_slow`, `weekly_sma10` columns from
  `attach_indicators` are there for that.

## Response style

- No conversational filler. Lead with the change.
- When proposing code, give a runnable block + the file path it lands in.
- Reference open items by their phrase from `TODO.md`, not invented IDs.
