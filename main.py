"""
TradAlert entry point. Fetches OHLCV, computes indicators, runs the two-stage
filter, persists run metadata, logs a structured report.

CLI
    python main.py              use cached data when fresh
    python main.py --force      bypass cache staleness and re-fetch
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

# ── path bootstrap ────────────────────────────────────────────────────────────
# Ensure src/ is on the Python path so this script is runnable from the CLI
# (python main.py) as well as from within the IDE with src/ as a source root.
# Mirrors the same pattern used in position_CLI.py (CODE-06 in TODO).
_SRC = Path(__file__).parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Load secrets.env before any module that reads os.environ.
load_dotenv(Path(__file__).parent / "config" / "secrets.env")

from persistence.cache import load as cache_load  # noqa: E402
from core.indicators.chart import chart  # noqa: E402
from persistence.db import save_scan_run, save_scan_results  # noqa: E402
from core.filter_engine import FilterEngine, ScanResult, SignalResult  # noqa: E402
from core.fetchers.fetcher import FetchSummary, fetch_watchlist, fetch_tier_b  # noqa: E402
from core.fetchers.earnings_fetcher import get_next_earnings  # noqa: E402
from core.fetchers.info_fetcher import get_market_cap  # noqa: E402
from core.fetchers.live_price import get_live_price  # noqa: E402
from core.indicators.indicators import attach_indicators  # noqa: E402
from core.position_manager import load_open_positions  # noqa: E402
from core.scoring import SignalScorer  # noqa: E402
from exceptions import InsufficientDataError  # noqa: E402

# Phase 2/5/6/8/9 imports (fail-open — modules exist but data may not)
try:
    from core.fetchers.macro import fetch_all_macro_series  # noqa: E402
    from core.macro import classify_macro_state, MacroState  # noqa: E402
    from core.macro.calendar import get_calendar_events  # noqa: E402
    from core.indicators.rp_rank import build_rp_rank_table  # noqa: E402
    from core.indicators.chart_signal_history import collect_signal_history  # noqa: E402

    _PHASE_MODULES_AVAILABLE = True
except ImportError:
    _PHASE_MODULES_AVAILABLE = False

# Phase 7 behavioral fetcher (fail-open)
try:
    from core.fetchers.behavioral import fetch_all_behavioral  # noqa: E402

    _BEHAVIORAL_AVAILABLE = True
except ImportError:
    _BEHAVIORAL_AVAILABLE = False

# ── paths ─────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent
_SETTINGS = _ROOT / "config" / "settings.yaml"
_FILTERS = _ROOT / "config" / "filters.yaml"
_WATCHLIST = _ROOT / "config" / "watchlist.yaml"
_LOG_FILE = _ROOT / "data" / "tradealert.log"

_MIN_ROWS: int = 20

_REGIME_INDICES: list[str] = ["SPY", "QQQ"]  # tradeable, scanned
_VIX_SYMBOL: str = "^VIX"  # context-only
_CONTEXT_ONLY: set[str] = {_VIX_SYMBOL}


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class TickerResult:
    """
    Per-ticker stage outcomes for one pipeline run.

    Attributes
    ----------
    ticker : Symbol.
    scan   : ScanResult; always present.
    signal : SignalResult, or None when scan failed or signal was skipped.
    error  : Non-empty when an unexpected exception occurred.
    """
    ticker: str
    scan: ScanResult
    signal: SignalResult | None = None
    error: str = ""


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Run the full pipeline for one scan. Exit 1 when no tickers were fetched."""
    args = _parse_args()
    settings = _load_settings()
    _setup_logging(settings)

    logger = logging.getLogger(__name__)
    logger.info("TradAlert starting")
    t0 = time.perf_counter()

    # ── 1. fetch ──────────────────────────────────────────────────────────────
    fetch_summary = fetch_watchlist(
        watchlist_path=_WATCHLIST,
        settings_path=_SETTINGS,
        force=args.force,
    )

    if not fetch_summary.succeeded:
        logger.error("No tickers fetched successfully — aborting pipeline.")
        sys.exit(1)

    # ── 1b. fetch tier_b universe (S&P 500 / TSX 60 constituents) ────────────
    try:
        tier_b_summary = fetch_tier_b(
            watchlist_path=_WATCHLIST,
            settings_path=_SETTINGS,
            force=args.force,
        )
        if tier_b_summary.total > 0:
            logger.info("[tier_b] %d / %d succeeded | %d failed | %.1fs",
                        tier_b_summary.n_succeeded, tier_b_summary.total,
                        tier_b_summary.n_failed, tier_b_summary.duration)
    except Exception as exc:
        logger.warning("[tier_b] fetch failed — proceeding without: %s", exc)

    # ── 2 – 6. context → enrich → scan → signal → score ─────────────────────
    # Parse filters.yaml once; pass the dict to both the engine and the scorer
    # so the file is not read and parsed a second time inside FilterEngine.__init__.
    filters_cfg = yaml.safe_load(_FILTERS.read_text(encoding="utf-8"))

    # Phase 5/6: Fetch macro series once (market-wide, reused across tickers)
    macro_series = {}
    macro_state = None
    if _PHASE_MODULES_AVAILABLE and settings.get("macro", {}).get("enabled", True):
        try:
            macro_series = fetch_all_macro_series(_SETTINGS, force=args.force)
            if macro_series:
                macro_state = classify_macro_state(macro_series, settings=settings)
                logger.info("[macro] risk_on=%.2f confidence=%.0f%%",
                            macro_state.risk_on_score, macro_state.confidence * 100)
        except Exception as exc:
            logger.warning("[macro] classification failed — proceeding without: %s", exc)

    # Phase 7/8: Fetch behavioral data once (market-wide, reused across tickers)
    behavioral_data = {}
    behavioral_state = None
    if _BEHAVIORAL_AVAILABLE and settings.get("behavioral", {}).get("enabled", True):
        try:
            behavioral_data = fetch_all_behavioral(_SETTINGS, force=args.force)
            if behavioral_data:
                from core.behavioral import classify_behavioral_state
                behavioral_state = classify_behavioral_state(
                    behavioral_data, settings=settings)
                logger.info("[behavioral] score=%.2f confidence=%.0f%%",
                            behavioral_state.behavioral_score,
                            behavioral_state.confidence * 100)
        except Exception as exc:
            logger.warning("[behavioral] classification failed — proceeding without: %s", exc)

    # Phase 8: Wire calendar events into engine (in-memory only)
    if _PHASE_MODULES_AVAILABLE:
        try:
            cal_events = get_calendar_events()
            # Extend engine._stop_dates with calendar events
            # (done after engine construction below)
        except Exception:
            cal_events = []
    else:
        cal_events = []

    engine = FilterEngine.from_dict(filters_cfg)

    # Inject calendar events into engine's stop_dates index
    if cal_events:
        import hashlib  # P1-8 FIX: deterministic IDs (hash() is per-run salt-randomized)
        for evt in cal_events:
            date_str = evt.date.isoformat()
            if date_str not in engine._stop_dates:
                stable_id = int(hashlib.sha256(date_str.encode()).hexdigest()[:8], 16) % 1000
                engine._stop_dates[date_str] = {
                    "id": 9000 + stable_id,
                    "date": date_str,
                    "description": f"{evt.category}: {evt.description}",
                    "action": evt.action,
                }

    scorer = SignalScorer(settings=settings, filters_cfg=filters_cfg)

    # Phase 2: Build RP rank table (cross-sectional, computed once)
    rp_ranks = {}
    if _PHASE_MODULES_AVAILABLE:
        try:
            rp_universe = {}
            wl_raw = yaml.safe_load(_WATCHLIST.read_text(encoding="utf-8"))
            # Load tier_a tickers for RP ranking
            tier_a = wl_raw.get("tier_a", []) if "tier_a" in wl_raw else wl_raw.get("tickers", [])
            for t in tier_a:
                if not isinstance(t, str):
                    continue
                if t in _CONTEXT_ONLY:
                    continue
                try:
                    rp_universe[t] = cache_load(t)
                except Exception:
                    pass
            if rp_universe:
                rp_ranks = build_rp_rank_table(rp_universe)
                logger.info("[rp_rank] built table for %d tickers", len(rp_ranks))
        except Exception as exc:
            logger.warning("[rp_rank] rank table build failed: %s", exc)

    results = _run_pipeline(
        fetch_summary.succeeded, engine, scorer,
        settings=settings,
        macro_state=macro_state, behavioral_state=behavioral_state,
        rp_ranks=rp_ranks,
    )

    elapsed = time.perf_counter() - t0

    # ── 7. persist ────────────────────────────────────────────────────────────
    _save_scan(fetch_summary, results, forced=args.force)

    # ── 8. report ─────────────────────────────────────────────────────────────
    _print_report(fetch_summary, results, total_seconds=elapsed)

    # ── 9. P1.9: alpha-decay watch ────────────────────────────────────────────
    _print_alpha_decay_watch()


# ── pipeline ──────────────────────────────────────────────────────────────────

def _run_pipeline(
        tickers: list[str],
        engine: FilterEngine,
        scorer: SignalScorer,
        settings: dict | None = None,
        macro_state: object | None = None,
        behavioral_state: object | None = None,
        rp_ranks: dict[str, float] | None = None,
) -> list[TickerResult]:
    """
    Run enrichment → scan → signal → score for every fetched ticker.

    Per-ticker steps:
        1. Load cached OHLCV from parquet
        2. Attach ATR, RSI, MACD, Bollinger
        3. Row-count guard (≥ _MIN_ROWS)
        4. Warmup guard (no NaN on last bar)
        5. Market-cap fetch (24h JSON cache, fail-open)
        6. FilterEngine.scan()
        7. FilterEngine.signal() — entry or exit mode based on positions
        8. Live price fetch (5-min cache, fail-open)  [entry signal path only]
        9. SignalScorer.enrich()

    Held positions always proceed to signal() regardless of scan outcome.
    Market context (SPY/QQQ/^VIX) and open positions are loaded once
    before the loop. Earnings dates are fetched per ticker on the entry
    path only.

    Parameters
    ----------
    tickers          : Symbols successfully fetched (FetchSummary.succeeded).
    engine           : Shared FilterEngine instance.
    scorer           : Shared SignalScorer instance.
    macro_state      : MacroState for regime size multiplier (Phase 6).
    behavioral_state : BehavioralState for regime size multiplier (Phase 8).
    rp_ranks         : Ticker → RP percentile rank [0, 99] (Phase 2).

    Returns
    -------
    list[TickerResult]
        One entry per ticker. Context-only tickers (^VIX) are skipped.
    """
    logger = logging.getLogger(__name__)
    results: list[TickerResult] = []
    settings = settings or {}

    # ── load market context and open positions once per run ──────────────────
    market_dfs, vix_df = _load_market_context(tickers)
    positions = load_open_positions()  # {ticker: Position}

    for ticker in tickers:
        # ^VIX is context-only — not tradeable, not scanned or signalled.
        if ticker in _CONTEXT_ONLY:
            logger.debug("[%s] skipping — market context only", ticker)
            continue

        logger.debug("Processing %s", ticker)
        held_position = positions.get(ticker)
        held_long = held_position is not None and held_position.side == "long"

        # ── 1. load cache ─────────────────────────────────────────────────────
        try:
            df = cache_load(ticker)
        except Exception as exc:
            logger.warning("[%s] cache load failed — %s", ticker, exc)
            results.append(TickerResult(
                ticker=ticker,
                scan=ScanResult(passed=False, reason="cache load failed"),
                error=str(exc),
            ))
            continue

        # ── 2. attach indicators ──────────────────────────────────────────────
        try:
            df = _attach_indicators(df)
        except Exception as exc:
            logger.warning("[%s] indicator computation failed — %s", ticker, exc)
            results.append(TickerResult(
                ticker=ticker,
                scan=ScanResult(passed=False, reason="indicator error"),
                error=str(exc),
            ))
            continue

        # ── 3. row-count guard ────────────────────────────────────────────────
        if len(df) < _MIN_ROWS:
            reason = f"only {len(df)} rows — need {_MIN_ROWS} for scan"
            logger.warning("[%s] skipping — %s", ticker, reason)
            results.append(TickerResult(
                ticker=ticker,
                scan=ScanResult(passed=False, reason=reason),
            ))
            continue

        # ── 4. warmup guard ───────────────────────────────────────────────────
        if not _indicators_ready(df):
            reason = "indicators still in warmup (NaN on last bar)"
            logger.warning("[%s] skipping — %s", ticker, reason)
            results.append(TickerResult(
                ticker=ticker,
                scan=ScanResult(passed=False, reason=reason),
            ))
            continue

        # ── 5. market-cap fetch (fail-open) ───────────────────────────────────
        # None skips the gate rather than blocking; equity gate only applies when
        # a value is returned.
        market_cap = None
        try:
            market_cap = get_market_cap(ticker)
        except Exception as exc:
            logger.warning("[%s] market-cap fetch failed (continuing) — %s",
                           ticker, exc)

        # ── 6. scan ───────────────────────────────────────────────────────────
        try:
            scan = engine.scan(ticker, df, market_cap=market_cap)
        except Exception as exc:
            logger.warning("[%s] scan raised — %s", ticker, exc)
            results.append(TickerResult(
                ticker=ticker,
                scan=ScanResult(passed=False, reason="scan exception"),
                error=str(exc),
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

        # ── 7. signal ─────────────────────────────────────────────────────────
        # Earnings buffer only applies to entries; exits skip the fetch.
        earnings_date = None
        if not held_long:
            try:
                earnings_date = get_next_earnings(ticker)
            except Exception as exc:
                logger.warning("[%s] earnings fetch failed (continuing) — %s",
                               ticker, exc)

        try:
            # Compute and enrich regime BEFORE signal() so behavioral gate applies
            regime = engine.market_regime(market_dfs, vix_df)
            if macro_state is not None or behavioral_state is not None:
                from dataclasses import replace
                regime = replace(
                    regime,
                    macro=macro_state,
                    behavioral=behavioral_state,
                )

            signal = engine.signal(
                ticker, df,
                market_dfs=market_dfs,
                vix_df=vix_df,
                earnings_date=earnings_date,
                held_long=held_long,
                regime=regime,
            )
        except InsufficientDataError as exc:
            logger.info("[%s] signal skipped — %s", ticker, exc)
            results.append(TickerResult(
                ticker=ticker,
                scan=scan,
                signal=SignalResult(
                    passed=False,
                    reason=f"insufficient data: {exc.detail}",
                ),
            ))
            continue
        except Exception as exc:
            logger.warning("[%s] signal raised — %s", ticker, exc)
            results.append(TickerResult(
                ticker=ticker,
                scan=scan,
                error=str(exc),
            ))
            continue

        # ── 8. live price + 9. score ──────────────────────────────────────────
        if signal.passed:
            # Live price fetch is fail-open — None omits current-price line
            live_price = None
            try:
                live_price = get_live_price(ticker)
            except Exception as exc:
                logger.debug("[%s] live price fetch failed — %s", ticker, exc)

            # regime already computed and enriched above
            scorer.enrich(
                signal=signal,
                df=df,
                regime=regime,
                earnings_date=earnings_date,
                position=held_position,
                market_dfs=market_dfs,
                vix_df=vix_df,
                current_price=live_price,
                rp_ranks=rp_ranks,
                ticker=ticker,
            )
            # Chart only for fire signals (score ≥ threshold, not watch-only)
            if not signal.watch_only:
                # Phase 9: Collect historical signals for chart overlay
                hist_signals = []
                if _PHASE_MODULES_AVAILABLE and settings.get("scanner", {}).get("chart", {}).get("signal_history",
                                                                                                 False):
                    try:
                        hist_signals = collect_signal_history(
                            ticker, df, engine, scorer,
                            market_dfs=market_dfs, vix_df=vix_df,
                            lookback=90,
                        )
                    except Exception as exc:
                        logger.debug("[%s] signal history collection failed: %s", ticker, exc)

                # Phase 9: Extract RP rank for scorecard
                ticker_rp = rp_ranks.get(ticker) if rp_ranks else None

                chart(
                    ticker, df, signal=signal,
                    output_dir=_ROOT / "data" / "screenshots",
                    historical_signals=hist_signals,
                    regime=regime,
                    score_components=getattr(signal, "score_components", None),
                    rp_rank=ticker_rp,
                )
        else:
            logger.debug("[%s] no signal — %s", ticker, signal.reason)

        results.append(TickerResult(ticker=ticker, scan=scan, signal=signal))

    return results


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_market_context(
        succeeded: list[str],
) -> tuple[dict[str, pd.DataFrame] | None, pd.DataFrame | None]:
    """
    Load SPY/QQQ (regime trend) and ^VIX (volatility) from cache.

    Returns
    -------
    market_dfs : Symbol → OHLCV mapping, or None when all loads failed.
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
    Return a copy of df with all standard indicator columns attached.

    Delegates to ``core.indicators.indicators.attach_indicators`` —
    the single canonical implementation shared with the backtester.

    Added columns: atr, rsi, macd, macd_signal, macd_hist,
                   bb_mid, bb_upper, bb_lower, bb_bw, bb_z.

    Parameters
    ----------
    df : Validated OHLCV DataFrame.

    Returns
    -------
    pd.DataFrame
    """
    return attach_indicators(df)


def _indicators_ready(df: pd.DataFrame) -> bool:
    """True when every indicator column is non-NaN on the last bar."""
    required = ["atr", "rsi", "macd", "macd_signal", "macd_hist"]
    return bool(df[required].iloc[-1].notna().all())


def _save_scan(
        fetch_summary: FetchSummary,
        results: list[TickerResult],
        forced: bool,
) -> None:
    """
    Persist one scan_runs row + scan_results rows.

    Counters
    --------
    tickers_scanned : Reached engine.scan() (excludes pre-scan failures).
    scan_passed     : ScanResult.passed is True.
    signals_fired   : SignalResult.passed is True.
    market_regime   : First non-empty regime label across results.
    """
    scan_passed = [r for r in results if r.scan.passed]
    scan_blocked = [r for r in results if not r.scan.passed and not r.error]
    signals = [r for r in scan_passed if r.signal and r.signal.passed]
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
    """Parse CLI args. ``--force`` bypasses cache staleness."""
    parser = argparse.ArgumentParser(
        prog="tradealert",
        description="TradAlert — fetch, enrich, scan, and signal the watchlist.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Bypass cache staleness check and re-fetch all tickers.",
    )
    return parser.parse_args()


def _load_settings() -> dict:
    """
    Load ``config/settings.yaml`` as a dict.

    Raises
    ------
    FileNotFoundError
        When settings.yaml does not exist at the expected path.
    """
    if not _SETTINGS.exists():
        raise FileNotFoundError(f"Settings file not found: {_SETTINGS}")
    return yaml.safe_load(_SETTINGS.read_text(encoding="utf-8"))


def _setup_logging(settings: dict) -> None:
    """
    Configure the root logger with stdout + ``data/tradealert.log`` handlers.

    Level read from ``storage.log_level`` (default INFO). Call once from main();
    repeated calls add duplicate handlers.
    """
    level_name: str = settings.get("storage", {}).get("log_level", "INFO").upper()
    level: int = getattr(logging, level_name, logging.INFO)

    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
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
        results: list[TickerResult],
        total_seconds: float = 0.0,
) -> None:
    """Log a structured pipeline summary: FETCH, SCAN, ENTRIES, EXITS, ERRORS."""
    logger = logging.getLogger(__name__)

    scan_passed = [r for r in results if r.scan.passed]
    scan_blocked = [r for r in results if not r.scan.passed and not r.error]
    signals = [r for r in results if r.signal and r.signal.passed]
    entries = [r for r in signals if r.signal.direction == "long"]
    exits = [r for r in signals if r.signal.direction == "exit_long"]
    fire_entries = [r for r in entries if not r.signal.watch_only]
    watch_entries = [r for r in entries if r.signal.watch_only]
    fire_exits = [r for r in exits if not r.signal.watch_only]
    watch_exits = [r for r in exits if r.signal.watch_only]
    errors = [r for r in results if r.error]

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

    logger.info(divider)
    logger.info("Done  %.1fs", total_seconds)


def _score_label(signal: SignalResult) -> str:
    """Return compact score string, e.g. '78/100'."""
    return f"{signal.score:.0f}/100"


def _print_alpha_decay_watch() -> None:
    """P1.9: Compute and display 6-month rolling E[R] from backtest trade CSV."""
    logger = logging.getLogger(__name__)
    try:
        import csv
        from datetime import date as _date
        from pathlib import Path

        csv_path = Path(__file__).parent / "data" / "backtest_out" / "trades.csv"
        if not csv_path.exists():
            logger.info("[alpha-decay] No trades.csv found — run a backtest first")
            return

        six_months_ago = _date.today() - __import__("datetime").timedelta(days=180)
        rs = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    exit_dt = _date.fromisoformat(row["exit_date"])
                    if exit_dt >= six_months_ago:
                        rs.append(float(row["r_multiple"]))
                except (ValueError, KeyError):
                    continue

        if not rs:
            logger.info("[alpha-decay] No trades in last 6 months — skipping")
            return

        rolling_er = sum(rs) / len(rs)
        logger.info(
            "[alpha-decay] 6-month rolling E[R]: %+.3f R (%d trades)",
            rolling_er, len(rs),
        )
        if rolling_er < 0:
            logger.warning(
                "[alpha-decay] CRITICAL: rolling E[R] < 0 — "
                "stop trading pending re-validation",
            )
        elif rolling_er < 0.05:
            logger.warning(
                "[alpha-decay] WARNING: rolling E[R] < +0.05 R — "
                "halve position size if sustained >8 weeks",
            )
    except Exception as exc:
        logger.debug("[alpha-decay] Could not compute rolling E[R]: %s", exc)


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
