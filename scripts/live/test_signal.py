#!/usr/bin/env python3
"""Read-only, single-ticker live signal and advisor probe.

Fetches current daily OHLCV directly from Yahoo, runs the same entry scan and
signal engine as the live scanner, and sends a fired entry to the configured
Ollama advisor. It never writes scan rows, application logs, price/fundamental/
news caches, charts, or Telegram messages; it never opens a database connection.

Usage:
    python scripts/live/test_signal.py AAPL
    python scripts/live/test_signal.py SHOP.TO --no-advisor
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / "config" / "secrets.env")
except ImportError:
    pass

from core.advisor import advise_signal, build_advisor_context
from core.behavioral import classify_behavioral_state
from core.fetchers.earnings_history import (
    fetch_earnings_dates_from_yfinance,
    next_earnings_from,
)
from core.fetchers.info_fetcher import fetch_market_cap
from core.fetchers.yf_fetchOne import fetch as fetch_ohlcv
from core.filter_engine import FilterEngine
from core.freshness import drop_unclosed_bar, exchange_for, sessions_behind
from core.macro import classify_macro_state
from core.types import ScanResult, SignalResult
from core.validators.dataframe_validator import validate_ohlcv
from exceptions import InsufficientDataError

def _config_regime_indices() -> tuple[str, ...]:
    """``filters.regime.index_symbols`` (fallback SPY/QQQ) — same knob the engine reads."""
    try:
        cfg = yaml.safe_load((_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8")) or {}
        idx = (cfg.get("regime") or {}).get("index_symbols")
        return tuple(str(s) for s in idx) if idx else ("SPY", "QQQ")
    except Exception:
        return ("SPY", "QQQ")


_REGIME_INDICES = _config_regime_indices()
_VIX_SYMBOL = "^VIX"


def _project_path(value: str | Path) -> Path:
    """Resolve a configured data path relative to the project root."""
    path = Path(value)
    return path if path.is_absolute() else _ROOT / path


def _load_parquet(path: Path) -> pd.DataFrame | None:
    """Read an existing parquet file only; missing/corrupt files stay absent."""
    try:
        return pd.read_parquet(path)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _fetch_completed_ohlcv(ticker: str, now: datetime) -> pd.DataFrame:
    """Fetch and validate current daily bars without touching the price cache."""
    df = validate_ohlcv(fetch_ohlcv(ticker), ticker=ticker)
    return drop_unclosed_bar(df, now, exchange_for(ticker))


def _load_market_context(
        now: datetime,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame | None, list[str]]:
    """Fetch SPY/QQQ/VIX directly, keeping all context data out of cache."""
    market_dfs: dict[str, pd.DataFrame] = {}
    unavailable: list[str] = []
    for ticker in _REGIME_INDICES:
        try:
            market_dfs[ticker] = _fetch_completed_ohlcv(ticker, now)
        except Exception:
            unavailable.append(ticker)

    vix_df = None
    try:
        vix_df = _fetch_completed_ohlcv(_VIX_SYMBOL, now)
    except Exception:
        unavailable.append(_VIX_SYMBOL)
    return market_dfs, vix_df, unavailable


def _load_macro_state(settings: dict) -> tuple[object | None, list[str]]:
    """Classify only macro files already on disk; this probe never refreshes them."""
    cfg = (settings.get("macro") or {})
    if not cfg.get("enabled", True):
        return None, []

    series_dir = _project_path(cfg.get("series_dir", "data/macro"))
    subset = set(cfg.get("series_subset") or ())
    series: dict[str, pd.DataFrame] = {}
    for key in (
            *cfg.get("fred_series", ()),
            *cfg.get("boc_series", ()),
            *cfg.get("yf_series", ()),
    ):
        if subset and key not in subset:
            continue
        df = _load_parquet(series_dir / f"{key}.parquet")
        if df is not None and not df.empty:
            series[key] = df

    if not series:
        return None, []
    try:
        return classify_macro_state(series, settings=settings), sorted(series)
    except Exception:
        return None, sorted(series)


def _drop_stale_behavioral(
        data: dict[str, pd.DataFrame], now: datetime, stale_days: float,
) -> tuple[dict, list[str]]:
    """Mirror the live stale-feed guard without attempting a refresh."""
    if stale_days <= 0:
        return data, []
    cutoff = pd.Timestamp(now.replace(tzinfo=None)) - pd.Timedelta(days=stale_days)
    fresh: dict[str, pd.DataFrame] = {}
    stale: list[str] = []
    for key, df in data.items():
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            continue
        last = pd.Timestamp(df.index[-1])
        if last.tzinfo is not None:
            last = last.tz_localize(None)
        if last < cutoff:
            stale.append(key)
        else:
            fresh[key] = df
    return fresh, stale


def _load_behavioral_state(
        settings: dict, now: datetime,
) -> tuple[object | None, list[str], list[str]]:
    """Classify existing behavioral cache files without creating or refreshing them."""
    cfg = (settings.get("behavioral") or {})
    if not cfg.get("enabled", True):
        return None, [], []

    data_dir = _project_path(cfg.get("data_dir", "data/behavioral"))
    paths = {
        "cot_es": data_dir / "cot_es.parquet",
        "breadth": data_dir / "sp500_breadth.parquet",
        "sector_rotation": data_dir / "sector_ratios.parquet",
    }
    data = {
        key: df for key, path in paths.items()
        if (df := _load_parquet(path)) is not None and not df.empty
    }
    fresh, stale = _drop_stale_behavioral(
        data, now, float(cfg.get("stale_window_days", 14)),
    )
    if not fresh:
        return None, [], stale
    try:
        return classify_behavioral_state(fresh, settings=settings), sorted(fresh), stale
    except Exception:
        return None, sorted(fresh), stale


def _evaluate_entry(
        ticker: str,
        df: pd.DataFrame,
        engine: FilterEngine,
        market_dfs: dict[str, pd.DataFrame],
        vix_df: pd.DataFrame | None,
        market_cap: float | None,
        earnings_date: date | None,
        macro_state: object | None,
        behavioral_state: object | None,
) -> tuple[ScanResult, SignalResult | None, object | None]:
    """Run the production entry path, stopping where a rejected scan stops."""
    scan = engine.scan(ticker, df, market_cap=market_cap)
    if not scan.passed:
        return scan, None, None

    regime = engine.market_regime(market_dfs, vix_df, empty_vote_trend="CHOP")
    if macro_state is not None or behavioral_state is not None:
        regime = replace(regime, macro=macro_state, behavioral=behavioral_state)
    signal = engine.signal(
        ticker,
        df,
        market_dfs=market_dfs,
        vix_df=vix_df,
        earnings_date=earnings_date,
        regime=regime,
        with_checks=True,
    )
    return scan, signal, regime


def _print_scan(scan: ScanResult) -> None:
    print(f"  SCAN     {'PASSED' if scan.passed else 'BLOCKED'}  {scan.reason}")
    if scan.close is not None:
        print(
            f"  DATA     close ${scan.close:.2f}  ATR {scan.atr:.2f} ({scan.atr_pct:.2f}%)"
            f"  RSI {scan.rsi:.1f}  DV20 ${scan.dv20:,.0f}"
        )
        if scan.market_cap is not None:
            print(f"  CAP      ${scan.market_cap:,.0f}")


def _print_signal(signal: SignalResult | None, regime: object | None) -> None:
    if signal is None:
        print("  SIGNAL   SKIPPED  scan blocked; production entry pipeline stops here")
        return
    if signal.passed:
        print(f"  SIGNAL   FIRED  {signal.signal_type} {signal.direction}")
        print(
            f"  RISK     stop ${signal.stop_price:.2f}  target ${signal.target_price:.2f}"
            f"  R:R {signal.min_rr:.1f}  size {signal.size_mult:.2f}"
        )
    else:
        print(f"  SIGNAL   NONE  {signal.reason}")
    print(f"  REGIME   {signal.market_regime or getattr(regime, 'label', 'N/A')}"
          f"  trend {signal.ticker_trend or 'N/A'}")
    if not signal.checks:
        return
    checks = "  ".join(
        f"{'PASS' if check.passed else 'FAIL'} {check.group}/{check.name}"
        + (f" ({check.detail})" if check.detail else "")
        for check in signal.checks
    )
    print(f"  CHECKS   {checks}")


def _dummy_signal(scan: ScanResult) -> SignalResult:
    """Minimal advisor input when the production entry path stopped at scan()."""
    return SignalResult(passed=False, reason=f"scan blocked: {scan.reason}")


def _advisor_note(
        ticker: str,
        signal: SignalResult,
        scan: ScanResult,
        df: pd.DataFrame,
        settings: dict,
        macro_state: object | None,
        behavioral_state: object | None,
        vix_df: pd.DataFrame | None,
) -> str:
    """Run the configured advisor without allowing its news cache to mutate."""

    advisor_settings = copy.deepcopy(settings)
    advisor_settings.setdefault("advisor", {})["enabled"] = True
    context = build_advisor_context(advisor_settings, read_only=True)
    if not context.enabled:
        return ""

    pct_from_ma = None
    try:
        ma_slow = float(df["ma_slow"].iloc[-1])
        close = float(df["close"].iloc[-1])
        if ma_slow:
            pct_from_ma = (close - ma_slow) / ma_slow * 100.0
    except (KeyError, IndexError, TypeError, ValueError):
        pass

    vix_level = None
    try:
        if vix_df is not None and not vix_df.empty:
            vix_level = float(vix_df["close"].iloc[-1])
    except (KeyError, IndexError, TypeError, ValueError):
        pass

    return advise_signal(
        ticker,
        signal,
        context,
        scan=scan,
        pct_from_ma=pct_from_ma,
        vix_level=vix_level,
        macro_score=getattr(macro_state, "risk_on_score", None),
        behavioral_score=getattr(behavioral_state, "behavioral_score", None),
        open_positions=0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only current-data signal and AI-advisor probe.",
    )
    parser.add_argument("ticker", help="Yahoo-compatible ticker, e.g. AAPL or SHOP.TO")
    parser.add_argument("--no-advisor", action="store_true",
                        help="Skip the Ollama advisor call")
    args = parser.parse_args()

    # This probe owns its output through print(); suppress all module loggers so it
    # cannot emit to an inherited handler or create an application log file.
    logging.disable(logging.CRITICAL)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ticker = args.ticker.upper()
    now = datetime.now(timezone.utc)
    started = time.perf_counter()
    settings = yaml.safe_load(
        (_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    filters = yaml.safe_load(
        (_ROOT / "config" / "filters.yaml").read_text(encoding="utf-8")) or {}
    engine = FilterEngine.from_dict(filters)

    print()
    print(f"  TEST SIGNAL  {ticker}  [read-only: no DB, logs, or project-cache writes]")
    print("  " + "-" * 68)

    try:
        df = _fetch_completed_ohlcv(ticker, now)
    except Exception as exc:
        print(f"  FETCH    FAILED  {type(exc).__name__}: {exc}")
        return 1
    if df.empty:
        print("  FETCH    FAILED  no completed daily bars")
        return 1
    print(f"  PRICE    Yahoo direct  last completed bar {df.index[-1].date()}  ({len(df)} rows)")

    min_rows = engine.cfg.trend.ma_slow
    try:
        from core.indicators.indicators import attach_indicators

        df = attach_indicators(df)
    except Exception as exc:
        print(f"  INDICATOR FAILED  {type(exc).__name__}: {exc}")
        return 1
    if len(df) < min_rows:
        print(f"  SCAN     BLOCKED  only {len(df)} rows; need {min_rows}")
        print("  SIGNAL   SKIPPED  insufficient history")
        return 0

    required = ["atr", "rsi", "macd", "macd_signal", "macd_hist"]
    if not bool(df[required].iloc[-1].notna().all()):
        print("  SCAN     BLOCKED  indicators still warming up")
        print("  SIGNAL   SKIPPED  incomplete indicators")
        return 0

    market_dfs, vix_df, unavailable_context = _load_market_context(now)
    macro_state, macro_loaded = _load_macro_state(settings)
    behavioral_state, behavioral_loaded, behavioral_stale = _load_behavioral_state(settings, now)
    context_symbols = list(market_dfs)
    if vix_df is not None:
        context_symbols.append(_VIX_SYMBOL)
    print("  CONTEXT  Yahoo direct " + (", ".join(context_symbols) or "unavailable"))
    if unavailable_context:
        print("  CONTEXT  unavailable " + ", ".join(unavailable_context))
    print("  MACRO    cached read-only " + (", ".join(macro_loaded) if macro_loaded else "unavailable"))
    print("  BEHAVIOR cached read-only " + (", ".join(behavioral_loaded) if behavioral_loaded else "unavailable"))
    if behavioral_stale:
        print("  BEHAVIOR stale and ignored " + ", ".join(behavioral_stale))

    market_cap = None
    try:
        market_cap = fetch_market_cap(ticker)
    except Exception as exc:
        print(f"  CAP      unavailable  {type(exc).__name__}: {exc}")

    earnings_date = None
    try:
        earnings_date = next_earnings_from(
            fetch_earnings_dates_from_yfinance(ticker), date.today(),
        )
    except Exception as exc:
        print(f"  EARNINGS unavailable  {type(exc).__name__}: {exc}")
    try:
        scan, signal, regime = _evaluate_entry(
            ticker,
            df,
            engine,
            market_dfs,
            vix_df,
            market_cap,
            earnings_date,
            macro_state,
            behavioral_state,
        )
    except InsufficientDataError as exc:
        scan = ScanResult(passed=False, reason=f"insufficient data: {exc.detail}")
        signal = None
        regime = None
    except Exception as exc:
        print(f"  PIPELINE FAILED  {type(exc).__name__}: {exc}")
        return 1

    _print_scan(scan)
    _print_signal(signal, regime)

    if args.no_advisor:
        print("  ADVISOR  SKIPPED  --no-advisor")
    else:
        advisor_signal = signal if signal is not None else _dummy_signal(scan)
        is_fired_entry = advisor_signal.passed and advisor_signal.direction in ("long", "short")
        if is_fired_entry:
            stale_sessions = sessions_behind(df.index[-1].date(), now, exchange_for(ticker))
            if stale_sessions:
                advisor_signal.tier = "NEEDS_REVIEW"
                advisor_signal.review_reason = f"stale {stale_sessions} session(s)"
        mode = "live entry" if is_fired_entry else "dummy no-signal context"
        print(f"  ADVISOR  {mode}; news fetched read-only; open positions fixed at 0")
        note = _advisor_note(
            ticker, advisor_signal, scan, df, settings, macro_state, behavioral_state, vix_df,
        )
        print(f"  ADVISOR  {note or 'no verdict (Ollama/news unavailable or response invalid)'}")

    print("  " + "-" * 68)
    print(f"  COMPLETE  {time.perf_counter() - started:.1f}s")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
