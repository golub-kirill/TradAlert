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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

# ── path bootstrap ────────────────────────────────────────────────────────────
# Ensure src/ is on the Python path so this script is runnable from the CLI
# (python main.py) as well as from within the IDE with src/ as a source root.
# Mirrors the same pattern used in position_CLI.py.
_SRC = Path(__file__).parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Load secrets.env before any module that reads os.environ.
load_dotenv(Path(__file__).parent / "config" / "secrets.env")

from persistence.cache import load as cache_load, get_or_fetch  # noqa: E402
from core.indicators.chart import chart  # noqa: E402
from core.freshness import (  # noqa: E402
    drop_unclosed_bar, exchange_for, overnight_gap, sessions_behind,
)
from core.fetchers.live_price import get_live_price  # noqa: E402
from core.fetchers.yf_fetchOne import fetch as _fetch_one  # noqa: E402
from persistence.db import save_scan_run, save_scan_results  # noqa: E402
from core.filter_engine import FilterEngine, GateCheck, ScanResult, SignalResult  # noqa: E402
from core.types import TickerResult  # noqa: E402
from core.fetchers.fetcher import FetchSummary, fetch_watchlist, fetch_tier_b  # noqa: E402
from core.fetchers.earnings_fetcher import get_next_earnings  # noqa: E402
from core.fetchers.info_fetcher import get_market_cap  # noqa: E402
from core.indicators.indicators import attach_indicators  # noqa: E402
from core.position_manager import load_open_positions  # noqa: E402
from core.exits import breakeven_stop_level, max_hold_exit_due  # noqa: E402
from exceptions import InsufficientDataError  # noqa: E402
from core.fetchers.http import mask_api_keys_filter  # noqa: E402

# (fail-open — modules exist but data may not)
try:
    from core.fetchers.macro import fetch_all_macro_series  # noqa: E402
    from core.macro import classify_macro_state  # noqa: E402
    from core.macro.calendar import get_calendar_events  # noqa: E402
    from core.indicators.rp_rank import build_rp_rank_table  # noqa: E402
    from core.indicators.chart_signal_history import collect_signal_history  # noqa: E402

    # Kept intentionally: graceful-import guard for the optional
    # modules (macro / calendar / rp_rank / signal_history). A partial or
    # stripped install degrades to long-only core instead of crashing at
    # import; the four consult sites below fall back when this is False.
    _PHASE_MODULES_AVAILABLE = True
except ImportError:
    _PHASE_MODULES_AVAILABLE = False

# behavioral fetcher (fail-open)
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


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Run the full pipeline for one scan. Exit 1 when no tickers were fetched."""
    # Force UTF-8 stdout/stderr so the report's box-drawing dividers (─) survive the
    # scheduler's cp1252 console — run_daily.bat redirects stdout/stderr into
    # logs/scheduler.log, where otherwise every divider raised UnicodeEncodeError and
    # spammed the log with logging-error tracebacks (the scan itself was unaffected).
    import sys as _sys
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

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

    # ── context → enrich → scan → signal ─────────────────────────────
    # Parse filters.yaml once and pass the dict to the engine so the file is
    # not read and parsed a second time inside FilterEngine.__init__.
    filters_cfg = yaml.safe_load(_FILTERS.read_text(encoding="utf-8"))

    # --allow-shorts flips the master switch on. We only mutate
    # when the flag is set, so an unflagged run leaves filters.yaml untouched
    # (allow_shorts defaults false there) and the long-only baseline replays
    # bit-identically.
    if args.allow_shorts:
        filters_cfg.setdefault("signals", {})["allow_shorts"] = True
        logger.info("[shorts] --allow-shorts enabled (signals.allow_shorts=true)")

    # Fetch macro series once (market-wide, reused across tickers)
    macro_series = {}
    macro_state = None
    if _PHASE_MODULES_AVAILABLE and settings.get("macro", {}).get("enabled", True):
        try:
            macro_series = fetch_all_macro_series(_SETTINGS, force=args.force)
            if macro_series:
                macro_state = classify_macro_state(macro_series, settings=settings)
                logger.info("[macro] risk_on=%.2f confidence=%.0f%%",
                            macro_state.risk_on_score, macro_state.confidence * 100)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("[macro] classification failed — proceeding without: %s",
                           exc, exc_info=True)

    # Fetch behavioral data once (market-wide, reused across tickers)
    behavioral_data = {}
    behavioral_state = None
    if _BEHAVIORAL_AVAILABLE and settings.get("behavioral", {}).get("enabled", True):
        try:
            behavioral_data = fetch_all_behavioral(_SETTINGS, force=args.force)
            if behavioral_data:
                from core.behavioral import classify_behavioral_state
                # LIVE staleness guard: drop feeds whose data-date is older than
                # behavioral.stale_window_days so a month-old cache degrades the
                # axis to missing (confidence falls) instead of sizing on it.
                stale_days = float(
                    (settings.get("behavioral", {}) or {}).get("stale_window_days", 14))
                behavioral_data = _drop_stale_behavioral(
                    behavioral_data, datetime.now(timezone.utc), stale_days)
                behavioral_state = classify_behavioral_state(
                    behavioral_data, settings=settings)
                logger.info("[behavioral] score=%.2f confidence=%.0f%%",
                            behavioral_state.behavioral_score,
                            behavioral_state.confidence * 100)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("[behavioral] classification failed — proceeding without: %s",
                           exc, exc_info=True)

    # Wire calendar events into engine (in-memory only)
    if _PHASE_MODULES_AVAILABLE:
        try:
            cal_events = get_calendar_events()
            # Extend engine._stop_dates with calendar events
            # (done after engine construction below)
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            logger.debug("[calendar] get_calendar_events failed (skipping): %s", exc)
            cal_events = []
    else:
        cal_events = []

    engine = FilterEngine.from_dict(filters_cfg)

    # Inject calendar events into engine's stop_dates index
    if cal_events:
        import hashlib
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

    # Build RP rank table (cross-sectional, computed once)
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
                except (FileNotFoundError, OSError, ValueError) as exc:
                    logger.debug("[rp_rank] cache_load skipped for %s: %s", t, exc)
            if rp_universe:
                rp_ranks = build_rp_rank_table(rp_universe)
                logger.info("[rp_rank] built table for %d tickers", len(rp_ranks))
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            logger.warning("[rp_rank] rank table build failed: %s", exc, exc_info=True)

    results = _run_pipeline(
        fetch_summary.succeeded, engine,
        settings=settings,
        macro_state=macro_state, behavioral_state=behavioral_state,
        rp_ranks=rp_ranks,
    )

    elapsed = time.perf_counter() - t0

    # ── 7. persist ────────────────────────────────────────────────────────────
    _save_scan(fetch_summary, results, forced=args.force, settings=settings)

    # ── 8. report ─────────────────────────────────────────────────────────────
    _print_report(fetch_summary, results, total_seconds=elapsed, settings=settings)

    # ── 8b. telegram push (fail-open; bit-neutral to the scan) ─────────────────
    # Off unless settings.telegram.enabled. Import inside the try so a missing
    # python-telegram-bot dep or any send error degrades to a log line and never
    # breaks the scan (mirrors the optional-import pattern above).
    try:
        from core.telegram.push import send_alerts
        send_alerts(results, settings, macro_state=macro_state)
    except Exception as exc:
        logging.getLogger(__name__).warning("[telegram] push skipped — %s", exc)

    # ── 8c. DB-health notice (fail-open, notification only) ────────────────────
    # If MySQL was unreachable, the scan fired with no open-position awareness and
    # left no journaled record. We do NOT block firing (fail-open by design) — we
    # alert the operator so a blind run doesn't pass silently.
    try:
        from core.position_manager import db_reachable
        if not db_reachable():
            from core.telegram.push import send_notice
            send_notice(
                "⚠️ TradAlert: MySQL was unreachable during this scan — it ran "
                "WITHOUT open-position awareness and was NOT journaled. Check the DB.",
                settings,
            )
            logging.getLogger(__name__).warning(
                "[db] DB unreachable during scan — operator notified; ran fail-open")
    except Exception as exc:
        logging.getLogger(__name__).warning("[db] health notice skipped — %s", exc)

    # ── 9. alpha-decay watch ────────────────────────────────────────────
    _print_alpha_decay_watch()


# ── pipeline ──────────────────────────────────────────────────────────────────

def _expected_hold_range(engine) -> tuple[int, int]:
    """Data-driven (low, high) expected-hold range for the chart/Telegram caption.

    Computed from the reference backtest's actual ``bars_held`` (25th-75th pct) so
    the displayed hold reflects reality, not a hand-set guess. Fail-open to a
    cap-anchored default (``execution.max_hold_days``) when the DB/backtest is
    unavailable. Display-only — no trade decision reads it.
    """
    cap = int(engine.cfg.execution.max_hold_days or 25)
    try:
        from backtest.db import expected_hold_range
        return expected_hold_range(cap=cap)
    except Exception as exc:
        logging.getLogger(__name__).debug("expected-hold range fell back — %s", exc)
        # Same research ratios as expected_hold_range's own fallback (cap 25 ≈ 3–14d).
        return (max(1, round(cap * 0.12)), max(1, round(cap * 0.56)))


def _maybe_raise_stop_to_breakeven(ticker, df, position, exec_cfg, settings) -> float | None:
    """Move a held position's stop to breakeven once its best excursion since entry
    reaches ``execution.breakeven_trigger_r`` — the live half of the rule the
    backtester applies in ``_apply_dynamic_stop`` (shared decision:
    ``core.exits.breakeven_stop_level``; ADR-004).

    A long stop ratchets UP to (around) entry, a short stop ratchets DOWN — in
    both directions the position's risk drops to ~0 without capping the upside.
    Returns the new stop level when the stop moved, else None. ``initial_stop`` is
    never touched (it stays the R denominator for reconciliation). Idempotent
    across daily scans: once the stop sits at/beyond breakeven this is a no-op.
    Fail-open — any error logs a warning and never blocks the scan.
    """
    logger = logging.getLogger(__name__)
    trigger = exec_cfg.breakeven_trigger_r
    side = position.side if position is not None else None
    if not trigger or side not in ("long", "short"):
        return None
    try:
        entry = float(position.entry_price)
        # initial_stop is immutable from open; stop_price is the documented
        # fallback for rows that predate the column.
        init_stop = position.initial_stop if position.initial_stop is not None \
            else position.stop_price
        if init_stop is None:
            return None
        init_stop = float(init_stop)
        # Risk is the entry→initial-stop distance; the short stop sits ABOVE entry.
        risk = (entry - init_stop) if side == "long" else (init_stop - entry)
        if risk <= 0:
            return None
        entry_pos = int(df.index.searchsorted(pd.Timestamp(position.entry_date)))
        if entry_pos >= len(df):
            return None
        # Best favorable excursion since entry (entry bar inclusive — matches the
        # backtester's update_excursion): highest high for a long, lowest low for
        # a short.
        if side == "long":
            mfe_r = (float(df["high"].iloc[entry_pos:].max()) - entry) / risk
        else:
            mfe_r = (entry - float(df["low"].iloc[entry_pos:].min())) / risk
        buffer = exec_cfg.breakeven_buffer_atr
        atr = None
        if buffer:
            _atr = df["atr"].iloc[-1] if "atr" in df.columns else None
            atr = float(_atr) if _atr is not None and pd.notna(_atr) else None
        new_stop = breakeven_stop_level(
            side=side, entry_price=entry, atr=atr,
            breakeven_trigger_r=float(trigger),
            breakeven_buffer_atr=float(buffer) if buffer else None,
            prev_stop=position.stop_price, initial_stop=init_stop,
            mfe_r=mfe_r,
        )
        if new_stop is None:
            return None
        current = position.stop_price
        if current is not None:
            current = float(current)
            # Long stop only moves up; short stop only moves down. No-op once the
            # stop already sits at/beyond breakeven.
            if side == "long" and new_stop <= current:
                return None
            if side == "short" and new_stop >= current:
                return None
        from core.position_manager import update_stop
        if not update_stop(position.id, float(new_stop)):
            logger.warning("[%s] breakeven stop update did not apply (id=%s)",
                           ticker, position.id)
            return None
        logger.info("[%s] stop moved to breakeven %.4f (MFE %.2fR ≥ %.2gR trigger)",
                    ticker, new_stop, mfe_r, float(trigger))
        try:
            from core.telegram.push import send_notice
            send_notice(
                f"🔒 {ticker}: stop moved to breakeven {new_stop:.2f} "
                f"(entry {entry:.2f}, best excursion {mfe_r:+.2f}R ≥ "
                f"{float(trigger):g}R trigger). Risk on this position is now ~0.",
                settings,
            )
        except Exception as exc:
            logger.debug("[%s] breakeven notice skipped — %s", ticker, exc)
        return float(new_stop)
    except Exception as exc:
        logger.warning("[%s] breakeven stop check failed — %s", ticker, exc)
        return None


def _append_live_context_checks(signal, ticker_rp, n_open, max_open_risk=None) -> None:
    """Fold live-only context into a fired entry's trigger-panel ``checks``.

    The engine builds the gate factors it can derive from market data; the live
    scanner knows more — the ticker's RP rank (LOCATION) and the open-risk budget
    consumed vs ``max_open_risk`` (CONTEXT). No-op for exits or when the engine
    did not emit checks, so the chart never disagrees with the real decision.
    """
    checks = getattr(signal, "checks", None)
    if not checks or signal.direction not in ("long", "short"):
        return
    is_long = signal.direction == "long"
    if ticker_rp is not None:
        try:
            rp = float(ticker_rp)
        except (TypeError, ValueError):
            rp = None
        if rp is not None:
            strength = max(0.0, min(1.0, rp / 100.0 if is_long else 1 - rp / 100.0))
            checks.append(GateCheck(
                group="LOCATION", name="RP", passed=strength >= 0.5,
                detail=f"{rp:.0f}", strength=strength))
    if n_open is not None:
        if max_open_risk:
            checks.append(GateCheck(
                group="CONTEXT", name="Budget", passed=n_open < max_open_risk,
                detail=f"{int(n_open)}/{max_open_risk:g}R"))
        else:
            checks.append(GateCheck(
                group="CONTEXT", name="Open", passed=True, detail=f"{int(n_open)} pos"))


def _run_pipeline(
        tickers: list[str],
        engine: FilterEngine,
        settings: dict | None = None,
        macro_state: object | None = None,
        behavioral_state: object | None = None,
        rp_ranks: dict[str, float] | None = None,
        now: datetime | None = None,
) -> list[TickerResult]:
    """
    Run enrichment → scan → signal for every fetched ticker.

    Per-ticker steps:
        1. Load cached OHLCV from parquet
        2. Attach ATR, RSI, MACD, Bollinger
        3. Row-count guard (≥ _MIN_ROWS)
        4. Warmup guard (no NaN on last bar)
        5. Market-cap fetch (24h JSON cache, fail-open)
        6. FilterEngine.scan()
        7. FilterEngine.signal() — entry or exit mode based on positions

    Held positions always proceed to signal() regardless of scan outcome.
    Market context (SPY/QQQ/^VIX) and open positions are loaded once
    before the loop. Earnings dates are fetched per ticker on the entry
    path only.

    Parameters
    ----------
    tickers          : Symbols successfully fetched (FetchSummary.succeeded).
    engine           : Shared FilterEngine instance.
    macro_state      : MacroState for regime size multiplier.
    behavioral_state : BehavioralState for regime size multiplier.
    rp_ranks         : Ticker → RP percentile rank [0, 99].

    Returns
    -------
    list[TickerResult]
        One entry per ticker. Context-only tickers (^VIX) are skipped.
    """
    logger = logging.getLogger(__name__)
    results: list[TickerResult] = []
    settings = settings or {}
    # One wall-clock stamp per scan → consistent freshness verdicts across tickers
    # (injectable so tests run the guards against their fixtures' dates, not real now).
    now = now or datetime.now(timezone.utc)

    # ── load market context and open positions once per run ──────────────────
    market_dfs, vix_df = _load_market_context(tickers, now=now)
    positions = load_open_positions()  # {ticker: Position}
    # Live open-risk budget (NORTH STAR): the validated portfolio caps aggregate
    # risk at max_open_risk and sizes each entry by size_mult. The scanner is an
    # alerter, so it surfaces budget consumed + size_mult rather than executing.
    max_open_risk = float((settings.get("risk") or {}).get("max_open_risk", 5.0))
    # Expected-hold range (display-only): the single source of truth, data-driven
    # from the reference backtest's actual bars_held (p25-p75). Computed once per
    # scan and applied to every fired entry. Fail-open.
    expected_hold = _expected_hold_range(engine)

    for ticker in tickers:
        # ^VIX is context-only — not tradeable, not scanned or signalled.
        if ticker in _CONTEXT_ONLY:
            logger.debug("[%s] skipping — market context only", ticker)
            continue
        results.append(_process_ticker(
            ticker, engine,
            positions=positions, market_dfs=market_dfs, vix_df=vix_df,
            max_open_risk=max_open_risk, expected_hold=expected_hold,
            settings=settings, macro_state=macro_state,
            behavioral_state=behavioral_state, rp_ranks=rp_ranks,
            now=now,
        ))

    return results


def _ensure_fresh(ticker: str, df: pd.DataFrame, now: datetime) -> tuple[pd.DataFrame, int]:
    """LIVE-only data-freshness guard: drop any unclosed current-day bar, then — if the data
    is still behind the last completed exchange session — force ONE refetch (refetch-first).

    Returns ``(df, stale_sessions)``. ``stale_sessions >= 1`` means the refetch could not
    freshen the data, so a fire from it must be reviewed, not LIVE. Fail-open (never raises).
    The backtester never calls this — it replays completed EOD bars by construction.
    """
    logger = logging.getLogger(__name__)
    exch = exchange_for(ticker)
    df = drop_unclosed_bar(df, now, exch)
    if len(df) == 0:
        return df, 0
    behind = sessions_behind(df.index[-1].date(), now, exch)
    if behind >= 1:
        try:
            fresh = get_or_fetch(ticker, _fetch_one, force=True)
            if fresh is not None and len(fresh):
                fresh = drop_unclosed_bar(fresh, now, exch)
                if len(fresh):
                    df = fresh
                    behind = sessions_behind(df.index[-1].date(), now, exch)
        except Exception as exc:
            logger.warning("[%s] stale-refetch failed (%d session(s) behind) — %s",
                           ticker, behind, exc)
    return df, behind


def _mark_review(ticker: str, signal: SignalResult, scan: ScanResult,
                 stale_sessions: int) -> None:
    """Downgrade a fired ENTRY to NEEDS_REVIEW (not LIVE) when its data is stale-after-refetch,
    the overnight/weekend gap breaches 2×ATR, or the gap could not be verified at all (no live
    quote). Mutates ``signal.tier``/``review_reason`` in place. The live price is fetched only
    for a fired entry with a valid close/ATR — cheap and fail-open (a missing live price never
    fabricates a breach, but it does flag the fire for review since the gap is unknown)."""
    logger = logging.getLogger(__name__)
    reasons: list[str] = []
    if stale_sessions >= 1:
        reasons.append(f"stale {stale_sessions} session" + ("s" if stale_sessions > 1 else ""))
    close, atr = scan.close, scan.atr
    if close and atr and close > 0 and atr > 0:
        try:
            live = get_live_price(ticker)
        except Exception:
            live = None
        if live is None:
            # No quote → the overnight/weekend gap is unknowable. Don't fabricate a
            # breach, but don't ship a blind LIVE alert either: flag it for review.
            logger.warning("[%s] no live quote — overnight gap unverified; "
                           "marking NEEDS_REVIEW", ticker)
            reasons.append("gap unverified — no live quote")
        else:
            gap, _pct, breached = overnight_gap(live, close, atr, atr_mult=2.0)
            if breached:
                reasons.append(f"gap {abs(gap) / atr:.1f}×ATR")
    if reasons:
        signal.tier = "NEEDS_REVIEW"
        signal.review_reason = " · ".join(reasons)


def _process_ticker(
        ticker: str,
        engine: FilterEngine,
        *,
        positions: dict,
        market_dfs: dict | None,
        vix_df,
        max_open_risk: float,
        expected_hold: tuple[int, int],
        settings: dict,
        macro_state: object | None,
        behavioral_state: object | None,
        rp_ranks: dict[str, float] | None,
        now: datetime | None = None,
) -> TickerResult:
    """Run scan → signal → (max-hold / breakeven / chart) for one ticker.

    Returns the ticker's TickerResult. Shared market context (market_dfs/vix_df),
    open positions, and the open-risk budget are loaded once per scan by
    _run_pipeline and passed in. Per-ticker steps and their fail-open contract are
    identical to the original inline loop body.
    """
    logger = logging.getLogger(__name__)
    held_position = positions.get(ticker)
    held_long = held_position is not None and held_position.side == "long"
    held_short = held_position is not None and held_position.side == "short"
    # Direction-neutral handles so the held-position branches below cover shorts
    # too (the backtester and telegram_bot already pass held_short — this closes
    # the live divergence). held_side is None for an unheld ticker (entry path).
    held_side = "long" if held_long else "short" if held_short else None
    held_exit_dir = "exit_long" if held_long else "exit_short" if held_short else None

    # ── 1. load cache ─────────────────────────────────────────────────────
    try:
        df = cache_load(ticker)
    except Exception as exc:
        logger.warning("[%s] cache load failed — %s", ticker, exc)
        return TickerResult(
            ticker=ticker,
            scan=ScanResult(passed=False, reason="cache load failed"),
            error=str(exc),
        )

    # ── 1b. live data-freshness guard (drop partial bar; refetch-first on stale) ──
    # LIVE path only — the backtester never reaches here. stale_sessions carries to the
    # fire below: a stale-after-refetch (or gapped) entry is downgraded to NEEDS_REVIEW.
    now = now or datetime.now(timezone.utc)
    df, stale_sessions = _ensure_fresh(ticker, df, now)
    if len(df) == 0:
        reason = "no completed sessions after freshness trim"
        logger.warning("[%s] skipping — %s", ticker, reason)
        return TickerResult(
            ticker=ticker,
            scan=ScanResult(passed=False, reason=reason),
        )

    # ── 2. attach indicators ──────────────────────────────────────────────
    try:
        df = _attach_indicators(df)
    except Exception as exc:
        logger.warning("[%s] indicator computation failed — %s", ticker, exc)
        return TickerResult(
            ticker=ticker,
            scan=ScanResult(passed=False, reason="indicator error"),
            error=str(exc),
        )

    # ── 3. row-count guard (matches engine scan()/signal() = trend.ma_slow) ──
    min_rows = engine.cfg.trend.ma_slow
    if len(df) < min_rows:
        reason = f"only {len(df)} rows — need {min_rows} for scan"
        logger.warning("[%s] skipping — %s", ticker, reason)
        return TickerResult(
            ticker=ticker,
            scan=ScanResult(passed=False, reason=reason),
        )

    # ── 4. warmup guard ───────────────────────────────────────────────────
    if not _indicators_ready(df):
        reason = "indicators still in warmup (NaN on last bar)"
        logger.warning("[%s] skipping — %s", ticker, reason)
        return TickerResult(
            ticker=ticker,
            scan=ScanResult(passed=False, reason=reason),
        )

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
        return TickerResult(
            ticker=ticker,
            scan=ScanResult(passed=False, reason="scan exception"),
            error=str(exc),
        )

    # Held positions always proceed to signal evaluation regardless of
    # scan outcome — we need to know whether to exit even if liquidity
    # or ATR ranges drifted out of the scan window.
    if not scan.passed and held_side is None:
        logger.debug("[%s] scan filtered — %s", ticker, scan.reason)
        return TickerResult(ticker=ticker, scan=scan)

    logger.debug("[%s] %s%s", ticker,
                 "HELD " if held_side else "",
                 "scan PASSED" if scan.passed else "scan filtered (held → proceed)")

    # ── 7. signal ─────────────────────────────────────────────────────────
    # Earnings buffer only applies to entries; exits skip the fetch.
    earnings_date = None
    if held_side is None:
        try:
            earnings_date = get_next_earnings(ticker)
        except Exception as exc:
            logger.warning("[%s] earnings fetch failed (continuing) — %s",
                           ticker, exc)

    try:
        # Compute and enrich regime BEFORE signal() so behavioral gate applies.
        # Live passes empty_vote_trend="CHOP": if the index caches are present but
        # unreadable (wipe / partial rebuild) the scanner blocks entries instead
        # of opening longs on a fail-open BULL.
        regime = engine.market_regime(market_dfs, vix_df, empty_vote_trend="CHOP")
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
            held_short=held_short,
            regime=regime,
            with_checks=True,
        )
    except InsufficientDataError as exc:
        logger.info("[%s] signal skipped — %s", ticker, exc)
        return TickerResult(
            ticker=ticker,
            scan=scan,
            signal=SignalResult(
                passed=False,
                reason=f"insufficient data: {exc.detail}",
            ),
        )
    except Exception as exc:
        logger.warning("[%s] signal raised — %s", ticker, exc)
        return TickerResult(
            ticker=ticker,
            scan=scan,
            error=str(exc),
        )

    # ── 7b. live max-hold (time-stop) exit ────────────────────────────────
    # Keep the live feed in step with the backtester's swing-horizon cap
    # (ADR-001): force an exit on a held position (long or short) that has
    # reached the cap and — in if_not_profit mode — is not in profit. Shares
    # core.exits with the backtester so live and backtest never diverge. Engine
    # exits take precedence (we only override a non-exit signal).
    if held_side and held_position is not None and signal.direction != held_exit_dir:
        _exec = engine.cfg.execution
        _mh_days = _exec.max_hold_days
        if _mh_days is not None:
            _mh_mode = str(_exec.max_hold_mode).replace("-", "_")
            _entry_pos = int(df.index.searchsorted(pd.Timestamp(held_position.entry_date)))
            if max_hold_exit_due(
                    bars_held=(len(df) - 1) - _entry_pos,
                    current_close=float(df["close"].iloc[-1]),
                    entry_price=held_position.entry_price, side=held_side,
                    max_hold_days=int(_mh_days), mode=_mh_mode):
                logger.info("[%s] max-hold time-stop exit (%d bars, %s)",
                            ticker, int(_mh_days), _mh_mode)
                signal = SignalResult(
                    passed=True, direction=held_exit_dir, signal_type="time_stop",
                    market_regime=signal.market_regime,
                    ticker_trend=signal.ticker_trend,
                    reason=f"max-hold {int(_mh_days)} bars reached "
                           f"({_mh_mode}) — time-stop exit",
                )

    # ── 7c. live breakeven stop ───────────────────────────────────────────
    # Mirror of the backtester's _apply_dynamic_stop breakeven rule
    # (core.exits.breakeven_stop_level, ADR-004): once a held position's best
    # excursion reaches the trigger, move positions.stop_price to entry (up for
    # a long, down for a short). Skipped when the position is exiting this scan
    # anyway. Fail-open.
    if held_side and held_position is not None and signal.direction != held_exit_dir:
        _maybe_raise_stop_to_breakeven(
            ticker, df, held_position,
            engine.cfg.execution, settings,
        )

    # ── 8. chart fired signals ────────────────────────────────────────────
    if signal.passed:
        # Data-driven expected-hold range for the chart/Telegram caption.
        # Entries only — exits don't display a hold horizon.
        if signal.direction in ("long", "short"):
            signal.expected_hold_days = expected_hold
            # Downgrade to NEEDS_REVIEW if the data was stale-after-refetch or gapped > 2×ATR.
            _mark_review(ticker, signal, scan, stale_sessions)

        # Collect historical signals for chart overlay
        hist_signals = []
        if _PHASE_MODULES_AVAILABLE and settings.get("scanner", {}).get("chart", {}).get("signal_history",
                                                                                         False):
            try:
                hist_signals = collect_signal_history(
                    ticker, df, engine,
                    market_dfs=market_dfs, vix_df=vix_df,
                    lookback=90,
                )
            except Exception as exc:
                logger.debug("[%s] signal history collection failed: %s", ticker, exc)

        # Extract RP rank for scorecard
        ticker_rp = rp_ranks.get(ticker) if rp_ranks else None

        # Trigger panel: fold live-only context (RP rank, open-risk budget)
        # into the engine's gate checks so the chart renders one unified,
        # direction-aware factor read.
        _append_live_context_checks(signal, ticker_rp, len(positions), max_open_risk)

        # Chart is display support — a render failure (disk full, codec,
        # OOM) must never kill the scan: the rest of the scan (journaling,
        # Telegram push) would be lost with it.
        try:
            chart(
                ticker, df, signal=signal,
                output_dir=_ROOT / "data" / "screenshots",
                historical_signals=hist_signals,
                regime=regime,
                rp_rank=ticker_rp,
            )
        except Exception as exc:
            logger.warning("[%s] chart render failed (alert still sent) — %s",
                           ticker, exc)
    else:
        logger.debug("[%s] no signal — %s", ticker, signal.reason)

    return TickerResult(ticker=ticker, scan=scan, signal=signal)


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_market_context(
        succeeded: list[str],
        now: datetime | None = None,
) -> tuple[dict[str, pd.DataFrame] | None, pd.DataFrame | None]:
    """
    Load SPY/QQQ (regime trend) and ^VIX (volatility) from cache.

    LIVE-only freshness: each context frame has any unclosed current-day bar
    dropped (``drop_unclosed_bar``) so the regime is classified on completed
    sessions only — an intraday/catch-up run never reads a partial index bar.
    The per-ticker path already does this via ``_ensure_fresh``; the backtester
    never calls this (it slices frames point-in-time with ``.loc[:D]``).

    Returns
    -------
    market_dfs : Symbol → OHLCV mapping, or None when all loads failed.
    vix_df     : VIX OHLCV, or None when absent/failed.
    """
    logger = logging.getLogger(__name__)
    now = now or datetime.now(timezone.utc)

    market_dfs: dict[str, pd.DataFrame] = {}
    for sym in _REGIME_INDICES:
        if sym not in succeeded:
            logger.warning("Regime index %s not in fetched tickers", sym)
            continue
        try:
            market_dfs[sym] = drop_unclosed_bar(cache_load(sym), now, exchange_for(sym))
        except Exception as exc:
            logger.warning("Failed to load regime index %s — %s", sym, exc)

    vix_df: pd.DataFrame | None = None
    if _VIX_SYMBOL in succeeded:
        try:
            vix_df = drop_unclosed_bar(
                cache_load(_VIX_SYMBOL), now, exchange_for(_VIX_SYMBOL))
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


def _feed_last_date(df) -> "pd.Timestamp | None":
    """Last timestamp of a feed frame (tz-stripped), or None for an empty frame
    or a non-datetime index."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex) or len(idx) == 0:
        return None
    last = idx[-1]
    return last.tz_localize(None) if last.tzinfo is not None else last


def _drop_stale_behavioral(data: dict, now: datetime, stale_days: float) -> dict:
    """LIVE-only: drop behavioral feeds whose latest data-date is more than
    ``stale_days`` behind ``now`` so the classifier treats that axis as MISSING
    (confidence falls) instead of sizing on month-old data. Returns a new dict of
    the fresh feeds and WARNs once per dropped feed. The backtester never calls
    this — it slices each feed point-in-time via ``as_of``."""
    if not data or stale_days <= 0:
        return data
    logger = logging.getLogger(__name__)
    now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
    cutoff = pd.Timestamp(now_naive) - pd.Timedelta(days=stale_days)
    fresh: dict = {}
    for key, df in data.items():
        last = _feed_last_date(df)
        if last is not None and last < cutoff:
            logger.warning(
                "[behavioral] feed %r STALE — last data %s, > %g days old; "
                "dropping (axis treated as missing, confidence falls).",
                key, last.date(), stale_days,
            )
            continue
        fresh[key] = df
    return fresh


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
        settings: dict | None = None,
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
        # POLICY: every scan must leave data for live reconciliation. A run_id
        # without the matching result rows (e.g. a missing scan_results column)
        # is just as blinding as no journal at all — surface a short insert
        # loudly instead of letting save_scan_results fail open in silence.
        inserted = save_scan_results(run_id, results)
        if results and inserted != len(results):
            msg = (f"Scan journal INCOMPLETE — {inserted}/{len(results)} rows "
                   f"written for run_id={run_id}. Live reconciliation will be "
                   f"blind to the missing fires; check the scan_results schema "
                   f"(tier/review_reason columns) and the DB error log.")
            print(f"  ⚠  {msg}")
            try:
                from core.telegram.push import send_notice
                send_notice(f"⚠️ TradAlert: {msg}", settings or {})
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "journal-incomplete notice skipped — %s", exc)
    else:
        # POLICY: every scan must leave data for live reconciliation. Make a
        # skipped journal loud rather than silent so the operator notices.
        print("  ⚠  Scan NOT journaled — DB unavailable. Set DB_* in "
              "config/secrets.env; live reconciliation depends on this feed.")


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
    parser.add_argument(
        "--allow-shorts",
        action="store_true",
        default=False,
        help="Enable short-side entries. Overrides "
             "signals.allow_shorts in filters.yaml to true. Default off "
             "keeps the long-only baseline replay-stable.",
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

    # Defensive: mask anything resembling an API key in formatted log output.
    _mask = mask_api_keys_filter()
    console.addFilter(_mask)
    file_h.addFilter(_mask)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_h)


# ── report ────────────────────────────────────────────────────────────────────

def _print_report(
        fetch_summary: FetchSummary,
        results: list[TickerResult],
        total_seconds: float = 0.0,
        *,
        settings: dict | None = None,
) -> None:
    """Log a structured pipeline summary: FETCH, SCAN, ENTRIES, EXITS, ERRORS."""
    logger = logging.getLogger(__name__)

    scan_passed = [r for r in results if r.scan.passed]
    scan_blocked = [r for r in results if not r.scan.passed and not r.error]
    # The ``r.signal is not None`` guard in each comprehension below is
    # redundant at runtime (the initial filter already excludes None) but
    # required by the type checker so subsequent ``.attribute`` accesses
    # narrow correctly. Without it PyCharm flagged 16 None-deref warnings
    # in this block — see TODO.md ``main.py None-deref guards``.
    signals = [r for r in results if r.signal is not None and r.signal.passed]
    entries = [r for r in signals
               if r.signal is not None and r.signal.direction == "long"]
    exits = [r for r in signals
             if r.signal is not None and r.signal.direction == "exit_long"]
    # Short-side counterparts. Empty unless --allow-shorts fired
    # short entries / exit_short covers, so the baseline summary is unchanged.
    short_entries = [r for r in signals
                     if r.signal is not None and r.signal.direction == "short"]
    short_exits = [r for r in signals
                   if r.signal is not None and r.signal.direction == "exit_short"]
    errors = [r for r in results if r.error]

    # NEEDS_REVIEW: fired entries whose data was stale-after-refetch or gapped > 2×ATR — pulled
    # into their own section (below). None flagged (the common post-close case) → the entries
    # lists are unchanged → byte-identical baseline report.
    review = [r for r in entries + short_entries
              if r.signal is not None and r.signal.tier == "NEEDS_REVIEW"]
    entries = [r for r in entries
               if r.signal is not None and r.signal.tier != "NEEDS_REVIEW"]
    short_entries = [r for r in short_entries
                     if r.signal is not None and r.signal.tier != "NEEDS_REVIEW"]

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

    # ── open-risk budget (NORTH STAR: live exposure must track the validated
    # portfolio, which caps aggregate risk and sizes by size_mult) ──────────────
    max_open_risk = float((settings or {}).get("risk", {}).get("max_open_risk", 5.0))
    n_open = None
    try:
        n_open = len(load_open_positions())
    except Exception:
        n_open = None
    budget_full = n_open is not None and n_open >= max_open_risk

    # ── entries ───────────────────────────────────────────────────────────────
    logger.info(divider)
    if entries:
        logger.info("ENTRIES  %d alert(s)", len(entries))
        if n_open is not None:
            status = ("BUDGET FULL — new entries exceed the validated risk cap"
                      if budget_full else f"room for ~{max_open_risk - n_open:.0f} more")
            logger.info("  RISK BUDGET  %d open / %.1f R  (%s)", n_open, max_open_risk, status)
        for r in entries:
            s = r.signal
            assert s is not None  # filtered by entries comprehension
            logger.info(
                "  ▲ %-10s  %-15s  size=%.2fx  stop=%-10.4f  target=%-10.4f"
                "  R:R≥%.1f  %s / %s%s",
                r.ticker, s.signal_type,
                float(s.size_mult), s.stop_price, s.target_price,
                s.min_rr, s.market_regime, s.ticker_trend,
                "  ⚠over-budget" if budget_full else "",
            )
    else:
        logger.info("ENTRIES  none")

    # ── short entries ──────────────────────────────────────────
    # Only rendered when short signals exist, so a long-only (baseline) run
    # produces byte-identical summary output.
    if short_entries:
        logger.info(divider)
        logger.info("SHORTS   %d entry alert(s)", len(short_entries))
        for r in short_entries:
            s = r.signal
            assert s is not None  # filtered by short_entries comprehension
            logger.info(
                "  ▼ %-10s  %-15s  size=%.2fx  stop=%-10.4f  target=%-10.4f"
                "  R:R≥%.1f  %s / %s%s",
                r.ticker, s.signal_type,
                float(s.size_mult), s.stop_price, s.target_price,
                s.min_rr, s.market_regime, s.ticker_trend,
                "  ⚠over-budget" if budget_full else "",
            )

    # ── needs review (stale / gapped data — verify before acting) ───────────────
    if review:
        logger.info(divider)
        logger.info("⚠ NEEDS REVIEW  %d alert(s) — data freshness, verify before acting",
                    len(review))
        for r in review:
            s = r.signal
            assert s is not None  # filtered by review comprehension
            arrow = "▼" if s.direction == "short" else "▲"
            logger.info(
                "  %s %-10s  %-15s  size=%.2fx  stop=%-10.4f  target=%-10.4f"
                "  R:R≥%.1f  %s / %s  [%s]",
                arrow, r.ticker, s.signal_type,
                float(s.size_mult), s.stop_price, s.target_price,
                s.min_rr, s.market_regime, s.ticker_trend, s.review_reason,
            )

    # ── exits ─────────────────────────────────────────────────────────────────
    if exits or short_exits:
        logger.info(divider)
    if exits:
        logger.info("EXITS    %d alert(s) — held longs", len(exits))
        for r in exits:
            s = r.signal
            assert s is not None  # filtered by exits comprehension
            logger.info(
                "  ✕ %-10s  %-15s  %s / %s  —  %s",
                r.ticker, s.signal_type,
                s.market_regime, s.ticker_trend, s.reason,
            )

    if short_exits:
        logger.info("COVERS   %d cover alert(s) for held shorts",
                    len(short_exits))
        for r in short_exits:
            s = r.signal
            assert s is not None  # filtered by short_exits comprehension
            logger.info(
                "  ✓ %-10s  %-15s  %s / %s  —  %s",
                r.ticker, s.signal_type,
                s.market_regime, s.ticker_trend, s.reason,
            )

    if errors:
        logger.info(divider)
        logger.info("ERRORS   %d ticker(s) raised exceptions", len(errors))
        for r in errors:
            logger.warning("  %-12s %s", r.ticker, r.error)

    logger.info(divider)
    logger.info("Done  %.1fs", total_seconds)


def _print_alpha_decay_watch() -> None:
    """Compute and display 6-month rolling E[R] from backtest trade CSV."""
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
