"""
TradAlert entry point. Fetches OHLCV, computes indicators, runs the two-stage
filter, persists run metadata, and logs a structured report.

CLI
    python main.py              use cached data when fresh
    python main.py --force      bypass cache staleness and re-fetch

Credentials are loaded from config/secrets.env before any module that reads
os.environ. Required keys: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

# Load secrets.env before any module that reads os.environ (db, position_manager).
# Resolve relative to this file so the script works from any working directory.
load_dotenv(Path(__file__).parent / "config" / "secrets.env")

from persistence.cache import load as cache_load  # noqa: E402
from core.indicators.chart import chart                            # noqa: E402
from persistence.db import save_scan_run, save_scan_results  # noqa: E402
from core.filter_engine import FilterEngine, ScanResult, SignalResult  # noqa: E402
from core.fetchers.fetcher import FetchSummary, fetch_watchlist    # noqa: E402
from core.fetchers.earnings_fetcher import get_next_earnings       # noqa: E402
from core.fetchers.info_fetcher import get_market_cap              # noqa: E402
from core.fetchers.live_price import get_live_price                # noqa: E402
from core.indicators.indicators import atr, bollinger_bands, macd, rsi              # noqa: E402
from core.position_manager import load_open_positions    # noqa: E402
from core.scoring import SignalScorer                              # noqa: E402
from exceptions import InsufficientDataError                       # noqa: E402


# ── paths ─────────────────────────────────────────────────────────────────────

_ROOT      = Path(__file__).parent
_SETTINGS  = _ROOT / "config" / "settings.yaml"
_FILTERS   = _ROOT / "config" / "filters.yaml"
_WATCHLIST = _ROOT / "config" / "watchlist.yaml"
_LOG_FILE  = _ROOT / "data"   / "tradealert.log"

# scan() needs 20 rows for the 20-day dollar-volume average.
# signal() needs trend.ma_slow (200) rows for MA200 — that guard lives inside
# FilterEngine and raises InsufficientDataError, which is caught below.
_MIN_ROWS: int = 20

# SPY/QQQ are tradeable so they remain in the signal loop.
# ^VIX is an index level only — skipped via _CONTEXT_ONLY.
_REGIME_INDICES: list[str] = ["SPY", "QQQ"]
_VIX_SYMBOL:     str       = "^VIX"
_CONTEXT_ONLY:   set[str]  = {_VIX_SYMBOL}


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class TickerResult:
    """
    Per-ticker stage outcomes for one pipeline run.

    Attributes
    ----------
    ticker : Symbol.
    scan   : ScanResult, always present.
    signal : SignalResult or None when scan failed or signal was skipped.
    error  : Non-empty when an unexpected exception occurred.
    """
    ticker: str
    scan:   ScanResult
    signal: SignalResult | None = None
    error:  str                 = ""


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """
    Orchestrate the full pipeline for one scan run.

    Exits with code 1 when no tickers were fetched successfully.
    """
    args     = _parse_args()
    settings = _load_settings()
    _setup_logging(settings)

    logger = logging.getLogger(__name__)
    logger.info("TradAlert starting")
    t0 = time.perf_counter()

    # ── 1. fetch ──────────────────────────────────────────────────────────────
    fetch_summary = fetch_watchlist(
        watchlist_path = _WATCHLIST,
        settings_path  = _SETTINGS,
        force          = args.force,
    )

    if not fetch_summary.succeeded:
        logger.error("No tickers fetched successfully — aborting pipeline.")
        sys.exit(1)

    # ── 2 – 6. context → enrich → scan → signal → score ─────────────────────
    filters_cfg = yaml.safe_load(_FILTERS.read_text())
    engine      = FilterEngine(config_path=_FILTERS)
    scorer      = SignalScorer(settings=settings, filters_cfg=filters_cfg)
    results     = _run_pipeline(fetch_summary.succeeded, engine, scorer)

    elapsed = time.perf_counter() - t0

    # ── 7. persist ────────────────────────────────────────────────────────────
    _save_scan(fetch_summary, results, forced=args.force)

    # ── 8. report ─────────────────────────────────────────────────────────────
    _print_report(fetch_summary, results, total_seconds=elapsed)


# ── pipeline ──────────────────────────────────────────────────────────────────

def _run_pipeline(
    tickers: list[str],
    engine:  FilterEngine,
    scorer:  SignalScorer,
) -> list[TickerResult]:
    """
    Run the enrichment → scan → signal → score pipeline for every fetched ticker.

    Per-ticker steps:
        1. Load cached OHLCV from parquet
        2. Attach ATR, RSI, MACD
        3. Row-count guard (≥ _MIN_ROWS)
        4. Warmup guard (no NaN on last bar)
        5. Market-cap fetch (24h JSON cache, fail-open)
        6. FilterEngine.scan()
        7. FilterEngine.signal() — entry or exit mode based on positions
        8. Live price fetch (5-min cache, fail-open)
        9. SignalScorer.enrich() — score, description, watch_only

    Held positions always proceed to signal() regardless of scan outcome.
    Market context (SPY / QQQ / ^VIX) and open positions are loaded once
    before the loop. Earnings dates are fetched per ticker on the entry
    path only (24h JSON cache).

    Parameters
    ----------
    tickers : Symbols successfully fetched, from FetchSummary.succeeded.
    engine  : Shared FilterEngine instance.
    scorer  : Shared SignalScorer instance.

    Returns
    -------
    list[TickerResult]
        One entry per ticker. Context-only tickers (^VIX) are skipped.
    """
    logger  = logging.getLogger(__name__)
    results: list[TickerResult] = []

    # ── load market context and open positions once per run ──────────────────
    market_dfs, vix_df = _load_market_context(tickers)
    positions          = load_open_positions()  # {ticker: Position}

    for ticker in tickers:
        # ^VIX is context-only — not tradeable, not scanned or signalled.
        if ticker in _CONTEXT_ONLY:
            logger.debug("[%s] skipping — market context only", ticker)
            continue

        logger.debug("Processing %s", ticker)
        held_position = positions.get(ticker)
        held_long     = held_position is not None and held_position.side == "long"

        # ── 1. load cache ─────────────────────────────────────────────────────
        try:
            df = cache_load(ticker)
        except Exception as exc:
            logger.warning("[%s] cache load failed — %s", ticker, exc)
            results.append(TickerResult(
                ticker = ticker,
                scan   = ScanResult(passed=False, reason="cache load failed"),
                error  = str(exc),
            ))
            continue

        # ── 2. attach indicators ──────────────────────────────────────────────
        try:
            df = _attach_indicators(df)
        except Exception as exc:
            logger.warning("[%s] indicator computation failed — %s", ticker, exc)
            results.append(TickerResult(
                ticker = ticker,
                scan   = ScanResult(passed=False, reason="indicator error"),
                error  = str(exc),
            ))
            continue

        # ── 3. row-count guard ────────────────────────────────────────────────
        if len(df) < _MIN_ROWS:
            reason = f"only {len(df)} rows — need {_MIN_ROWS} for scan"
            logger.warning("[%s] skipping — %s", ticker, reason)
            results.append(TickerResult(
                ticker = ticker,
                scan   = ScanResult(passed=False, reason=reason),
            ))
            continue

        # ── 4. warmup guard ───────────────────────────────────────────────────
        if not _indicators_ready(df):
            reason = "indicators still in warmup (NaN on last bar)"
            logger.warning("[%s] skipping — %s", ticker, reason)
            results.append(TickerResult(
                ticker = ticker,
                scan   = ScanResult(passed=False, reason=reason),
            ))
            continue

        # ── 5. scan ───────────────────────────────────────────────────────────
        # Market-cap fetch is fail-open: None skips the gate rather than blocks.
        market_cap = None
        try:
            market_cap = get_market_cap(ticker)
        except Exception as exc:
            logger.warning("[%s] market-cap fetch failed (continuing) — %s",
                           ticker, exc)

        try:
            scan = engine.scan(ticker, df, market_cap=market_cap)
        except Exception as exc:
            logger.warning("[%s] scan raised — %s", ticker, exc)
            results.append(TickerResult(
                ticker = ticker,
                scan   = ScanResult(passed=False, reason="scan exception"),
                error  = str(exc),
            ))
            continue

        # Held positions always proceed to signal evaluation regardless of
        # scan outcome — we need to know whether to exit even if liquidity
        # or ATR ranges drifted out of the scan window.
        if not scan.passed and not held_long:
            logger.debug("[%s] scan filtered — %s", ticker, scan.reason)
            results.append(TickerResult(ticker=ticker, scan=scan))
            continue

        logger.debug("[%s] %s%s", ticker,
                     "HELD " if held_long else "",
                     "scan PASSED" if scan.passed else "scan filtered (held → proceed)")

        # ── 6. signal ─────────────────────────────────────────────────────────
        # Earnings buffer only applies to entries; exits skip the fetch.
        earnings_date = None
        if not held_long:
            try:
                earnings_date = get_next_earnings(ticker)
            except Exception as exc:
                logger.warning("[%s] earnings fetch failed (continuing) — %s",
                               ticker, exc)

        try:
            signal = engine.signal(
                ticker, df,
                market_dfs    = market_dfs,
                vix_df        = vix_df,
                earnings_date = earnings_date,
                held_long     = held_long,
            )
        except InsufficientDataError as exc:
            logger.info("[%s] signal skipped — %s", ticker, exc)
            results.append(TickerResult(
                ticker = ticker,
                scan   = scan,
                signal = SignalResult(
                    passed = False,
                    reason = f"insufficient data: {exc.detail}",
                ),
            ))
            continue
        except Exception as exc:
            logger.warning("[%s] signal raised — %s", ticker, exc)
            results.append(TickerResult(
                ticker = ticker,
                scan   = scan,
                error  = str(exc),
            ))
            continue

        # ── 8. score ──────────────────────────────────────────────────────────
        if signal.passed:
            # Live price fetch is fail-open — None omits current-price line
            live_price = None
            try:
                live_price = get_live_price(ticker)
            except Exception as exc:
                logger.debug("[%s] live price fetch failed — %s", ticker, exc)

            regime = engine.market_regime(market_dfs, vix_df)
            scorer.enrich(
                signal        = signal,
                df            = df,
                regime        = regime,
                earnings_date = earnings_date,
                position      = held_position,
                market_dfs    = market_dfs,
                vix_df        = vix_df,
                current_price = live_price,
            )
            # Chart only for fire signals (score ≥ threshold, not watch-only)
            if not signal.watch_only:
                chart(ticker, df, signal=signal,
                      output_dir=_ROOT / "data" / "screenshots")
        else:
            logger.debug("[%s] no signal — %s", ticker, signal.reason)

        results.append(TickerResult(ticker=ticker, scan=scan, signal=signal))

    return results


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_market_context(
    succeeded: list[str],
) -> tuple[dict[str, pd.DataFrame] | None, pd.DataFrame | None]:
    """
    Load SPY / QQQ (regime trend) and ^VIX (volatility regime) from cache.

    Both loads are best-effort. Missing indices → _market_regime() falls
    back to BULL. Missing VIX → volatility defaults to NORMAL.

    Returns
    -------
    market_dfs : Symbol → OHLCV mapping, or None when all failed.
    vix_df     : VIX OHLCV, or None when absent/failed.
    """
    logger = logging.getLogger(__name__)

    market_dfs: dict[str, pd.DataFrame] = {}
    for sym in _REGIME_INDICES:
        if sym not in succeeded:
            logger.warning("Regime index %s not in fetched tickers", sym)
            continue
        try:
            market_dfs[sym] = cache_load(sym)
        except Exception as exc:
            logger.warning("Failed to load regime index %s — %s", sym, exc)

    vix_df: pd.DataFrame | None = None
    if _VIX_SYMBOL in succeeded:
        try:
            vix_df = cache_load(_VIX_SYMBOL)
        except Exception as exc:
            logger.warning("Failed to load %s — %s", _VIX_SYMBOL, exc)
    else:
        logger.warning(
            "%s not in fetched tickers — volatility defaults to NORMAL",
            _VIX_SYMBOL,
        )

    if market_dfs:
        logger.debug(
            "Market context loaded: %s + VIX=%s",
            list(market_dfs.keys()),
            "yes" if vix_df is not None else "no",
        )

    return (market_dfs or None), vix_df


def _attach_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a copy of df with ATR/RSI/MACD columns attached.

    Added columns: atr, rsi, macd, macd_signal, macd_hist.
    All computed on the full history so the last bar has no warmup NaN.

    Parameters
    ----------
    df : Validated OHLCV DataFrame with columns open/high/low/close/volume.

    Returns
    -------
    pd.DataFrame
    """
    df = df.copy()

    df["atr"] = atr(df)
    df["rsi"] = rsi(df["close"])

    macd_line, signal_line, histogram = macd(df["close"])
    df["macd"]        = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"]   = histogram

    bb = bollinger_bands(df["close"])
    df["bb_mid"]   = bb["bb_mid"]
    df["bb_upper"] = bb["bb_upper"]
    df["bb_lower"] = bb["bb_lower"]
    df["bb_bw"]    = bb["bb_bw"]
    df["bb_z"]     = bb["bb_z"]

    return df


def _indicators_ready(df: pd.DataFrame) -> bool:
    """
    True when every indicator column is non-NaN on the last bar.

    A NaN means an EWM has not warmed up — filter results would be undefined.
    """
    required = ["atr", "rsi", "macd", "macd_signal", "macd_hist"]
    return bool(df[required].iloc[-1].notna().all())


def _save_scan(
    fetch_summary: FetchSummary,
    results:       list[TickerResult],
    forced:        bool,
) -> None:
    """
    Persist one scan_runs row to MySQL after the pipeline has completed.

    Counter definitions match _print_report() so log and DB agree.

    tickers_scanned : Reached engine.scan() (excludes pre-scan failures).
    scan_passed     : ScanResult.passed is True.
    signals_fired   : SignalResult.passed is True.
    market_regime   : First non-empty regime label across results.

    Fail-open: save_scan_run() catches MySQLError internally.
    """
    scan_passed     = [r for r in results if r.scan.passed]
    scan_blocked    = [r for r in results if not r.scan.passed and not r.error]
    signals         = [r for r in scan_passed if r.signal and r.signal.passed]
    tickers_scanned = len(scan_passed) + len(scan_blocked)

    market_regime: str | None = next(
        (
            r.signal.market_regime
            for r in results
            if r.signal and r.signal.market_regime
        ),
        None,
    )


    run_id = save_scan_run(
        forced=forced,
        tickers_attempted=fetch_summary.total,
        tickers_fetched=fetch_summary.n_succeeded,
        tickers_scanned=tickers_scanned,
        scan_passed=len(scan_passed),
        signals_fired=len(signals),
        market_regime=market_regime,
    )

    if run_id:
        save_scan_results(run_id, results)


def _parse_args() -> argparse.Namespace:
    """
    Parse CLI args.

    --force : Bypass cache staleness check and re-fetch all tickers.
    """
    parser = argparse.ArgumentParser(
        prog        = "tradealert",
        description = "TradAlert — fetch, enrich, scan, and signal the watchlist.",
    )
    parser.add_argument(
        "--force",
        action  = "store_true",
        default = False,
        help    = "Bypass cache staleness check and re-fetch all tickers.",
    )
    return parser.parse_args()


def _load_settings() -> dict:
    """
    Load config/settings.yaml as a dict.

    Called before logging is configured — a missing file surfaces as an
    unformatted exception, which is the intended failure mode.

    Raises
    ------
    FileNotFoundError
        When settings.yaml does not exist at the expected path.
    """
    if not _SETTINGS.exists():
        raise FileNotFoundError(f"Settings file not found: {_SETTINGS}")
    return yaml.safe_load(_SETTINGS.read_text())


def _setup_logging(settings: dict) -> None:
    """
    Configure the root logger with stdout + data/tradealert.log handlers.

    Level read from settings.yaml → storage.log_level (default INFO).
    Call exactly once from main(); a second call adds duplicate handlers.
    """
    level_name: str = settings.get("storage", {}).get("log_level", "INFO").upper()
    level: int      = getattr(logging, level_name, logging.INFO)

    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt       = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt   = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_h = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_h)


# ── report ────────────────────────────────────────────────────────────────────

def _print_report(
    fetch_summary: FetchSummary,
    results:       list[TickerResult],
    total_seconds: float = 0.0,
) -> None:
    """
    Log a structured pipeline summary at the end of the run.

    Sections: FETCH, SCAN, SIGNALS, ERRORS.
    """
    logger = logging.getLogger(__name__)

    scan_passed  = [r for r in results if r.scan.passed]
    scan_blocked = [r for r in results if not r.scan.passed and not r.error]
    signals      = [r for r in results if r.signal and r.signal.passed]
    entries      = [r for r in signals  if r.signal.direction == "long"]
    exits        = [r for r in signals  if r.signal.direction == "exit_long"]
    fire_entries = [r for r in entries  if not r.signal.watch_only]
    watch_entries= [r for r in entries  if r.signal.watch_only]
    fire_exits   = [r for r in exits    if not r.signal.watch_only]
    watch_exits  = [r for r in exits    if r.signal.watch_only]
    errors       = [r for r in results  if r.error]

    divider = "─" * 72

    logger.info(divider)
    logger.info(
        "FETCH    %d / %d succeeded  |  %d failed",
        fetch_summary.n_succeeded, fetch_summary.total, fetch_summary.n_failed,
    )
    if fetch_summary.failed:
        for ticker, reason in sorted(fetch_summary.failed.items()):
            logger.info("  ✗ %-12s %s", ticker, reason)

    logger.info(divider)
    logger.info(
        "SCAN     %d passed  |  %d filtered  |  %d errors",
        len(scan_passed), len(scan_blocked), len(errors),
    )
    for r in scan_blocked:
        logger.debug("  filtered  %-12s %s", r.ticker, r.scan.reason)

    # ── entries ───────────────────────────────────────────────────────────────
    logger.info(divider)
    if fire_entries:
        logger.info("ENTRIES  %d alert(s)", len(fire_entries))
        for r in fire_entries:
            s = r.signal
            logger.info(
                "  ▲ %-10s  %-15s  score=%s  stop=%-10.4f  target=%-10.4f"
                "  R:R≥%.1f  %s / %s",
                r.ticker, s.signal_type,
                _score_label(s), s.stop_price, s.target_price,
                s.min_rr, s.market_regime, s.ticker_trend,
            )
            for line in s.description.splitlines():
                logger.info("    %s", line)
    else:
        logger.info("ENTRIES  none")

    if watch_entries:
        logger.info("  — WATCH (score below threshold) —")
        for r in watch_entries:
            s = r.signal
            logger.info(
                "  ~ %-10s  %-15s  score=%s  stop=%-10.4f  %s / %s",
                r.ticker, s.signal_type,
                _score_label(s), s.stop_price,
                s.market_regime, s.ticker_trend,
            )
            for line in s.description.splitlines():
                logger.info("    %s", line)

    # ── exits ─────────────────────────────────────────────────────────────────
    if fire_exits or watch_exits:
        logger.info(divider)
    if fire_exits:
        logger.info("EXITS    %d alert(s) — held longs", len(fire_exits))
        for r in fire_exits:
            s = r.signal
            logger.info(
                "  ✕ %-10s  %-15s  score=%s  %s / %s  —  %s",
                r.ticker, s.signal_type,
                _score_label(s), s.market_regime, s.ticker_trend, s.reason,
            )
            for line in s.description.splitlines():
                logger.info("    %s", line)

    if watch_exits:
        logger.info("  — WATCH exits (score below threshold) —")
        for r in watch_exits:
            s = r.signal
            logger.info(
                "  ~ %-10s  %-15s  score=%s  %s / %s  —  %s",
                r.ticker, s.signal_type,
                _score_label(s), s.market_regime, s.ticker_trend, s.reason,
            )

    if errors:
        logger.info(divider)
        logger.info("ERRORS   %d ticker(s) raised exceptions", len(errors))
        for r in errors:
            logger.warning("  %-12s %s", r.ticker, r.error)

    if errors:
        logger.info(divider)
        logger.info("ERRORS   %d ticker(s) raised exceptions", len(errors))
        for r in errors:
            logger.warning("  %-12s %s", r.ticker, r.error)

    logger.info(divider)
    logger.info("Done  %.1fs", total_seconds)


def _score_label(signal: SignalResult) -> str:
    """Return compact score string, e.g. '78/100'."""
    return f"{signal.score:.0f}/100"


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
