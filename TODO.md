# TODO

Open backlog after the recent cleanup pass. Read "State of play" first.

## State of play

- All 65 production .py files parse and import.
- All four CLIs start: `main.py`, `position_CLI.py`,
  `backtest/run_backtest.py`, `backtest/repair_parquet.py`.
- `--journal` no longer crashes when DB env vars are missing; logs
  "Journal skipped — DB env vars not set" and continues.
- Kelly + Monte-Carlo report blocks display sizing at **1% risk per R**
  (was 5%); Kelly fractions kept as reference only.
- Signal gates widened to surface more entries on the 100-ticker watchlist:
    - `signals.momentum.long.rsi_max`: 65 → 70
    - `signals.mean_reversion.long.rsi_max`: 35 → 70
    - `signals.mean_reversion.long.min_hist_delta_atr`: 0.18 → 0.08
- The following fetchers are minimal-but-importable stubs after the
  indentation-recovery — restore original logic from local IDE history
  (or git stash) before next production scan:
    - `src/core/fetchers/behavioral/aaii.py`
    - `src/core/fetchers/behavioral/cot.py`
    - `src/core/fetchers/behavioral/naaim.py`
    - `src/core/fetchers/behavioral/short_interest.py`
    - `src/core/fetchers/behavioral/form4.py` (returns zeros)
    - `src/core/fetchers/behavioral/breadth.py` (rewritten; verify the
      100-constituent perf truncation is still acceptable)
    - `src/core/macro/calendar.py` (returns `[]`)
      Stubs return empty/zero data which classifiers treat as "axis missing"
      — pipeline runs, just with less signal richness.
- `tests/test_regression_fixes.py` is stale (many tests reference
  audit-tracker tokens that were stripped). Re-author or delete before
  enabling CI.

## Correctness

- Restore behavioral fetcher implementations (see list above).
- Survivorship bias: S&P 500 / TSX 60 fetchers pull *current* membership.
  Load date-stamped historical membership; filter per backtest window.
- Form 4 XML parser: distinguish buys (`P`) vs sells (`S`), aggregate
  $ values. `SignalScorer` refuses to start if `weights.insider_buying > 0`.
- Target-side gap-fill: add `apply_target_fill = max(target, bar_open)`
  symmetric to `apply_stop_fill`.
- Brent-as-WCS proxy: `BZ=F` is not WCS-CAD; replace or drop the axis.
- Walk-forward best-config currently uses single-best parameter from the
  OFAT grid; switch to joint-optimal or rename the metric.
- Behavioral sentiment z-score: rolling 52-week window, not full-series.
- Loader robustness: filter out non-DataFrame entries from
  `behavioral_data` (the `Path` subdir keys).
- Currency policy: USD-only, per-currency liquidity thresholds, or daily
  FX conversion — pick one.

## Signal quality

- Re-tune after gate widening. With `mean_reversion.long.rsi_max=70` and
  `min_hist_delta_atr=0.08`, MR is online again. Run `--sweep` and confirm
  MR trades have positive E[R].
- `Trade.compute_r` uses slipped entry but `initial_target` is set from
  pre-slippage close — actual R diverges slightly from configured `min_rr`.
- `compute_vbp` puts `close × volume` in the close bin (not classic H-L
  spread). Rename or rewrite.

## Performance

- `_pack_universe` pickles ~75 MB × N workers per sweep. Move to
  `multiprocessing.shared_memory`.
- `compute_sp500_breadth` truncates to the first 100 alphabetical
  constituents. Thread the full universe or document the bias.
- Walk-forward sweep cache key should include parameter-grid hash.

## Portfolio / config

- `max_concurrent_per_sector` in `PortfolioConfig`; consult
  `config/sector_map.yaml`.
- Dynamic `expected_hold_days` derived from observed median hold per
  signal_type from `data/backtest_out/trades.csv`.
- Watchlist diversity: 84 / 100 tickers have data in the current run.

## Operational

- `position_CLI.py open --date YYYY-MM-DD` for retroactive opens.
- Short trading: requires changes in `position_manager`, `FilterEngine`,
  signal scoring, backtester fills, `apply_stop_fill`. Only `--side long`
  is wired through today.
- Pin dependencies in `requirements.txt` (currently unpinned).
- Capture MySQL CREATE statements (`scan_runs`, `scan_results`,
  `backtest_runs`, `backtest_trades`, `positions`) in
  `data/backtest_schema.sql`.

## Architecture

- `FilterEngine` god-class (1k+ LOC). Split into `ConfigLoader` +
  `Scanner` + `SignalEngine`.
- `main.py` god-module (800+ LOC). Split orchestration / persistence /
  CLI / reporting.
- Migrate 65+ bare-string comparisons (`signal.direction == "long"`) to
  `core.types.SIGNAL_TYPE` / `DIRECTION` / `TICKER_TREND` constants.
- Introduce `ApplicationContext` for dependency injection; each component
  currently re-reads YAML at construction.
- `backtest/sweep.py` is 990 LOC. Split engine / grid / worker / result.
- Unused locals (`trough_eq`, `distinct_30`, `breadth_records`) — wire
  up.

