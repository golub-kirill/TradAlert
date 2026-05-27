"""
Threaded watchlist runner.

Loads the watchlist from config/watchlist.yaml, fetches and caches OHLCV
for every ticker in parallel via ThreadPoolExecutor. A single failed
ticker is skipped, logged as WARNING, and recorded in FetchSummary.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import partial
from pathlib import Path

import yaml

from core.fetchers import yf_fetchOne
from core.fetchers.yf_fetchOne import DEFAULT_INTERVAL, DEFAULT_LOOKBACK
from core.paths import CONFIG_DIR, WATCHLIST_YAML, SETTINGS_YAML
from exceptions import ConfigError
from persistence.cache import DEFAULT_STALENESS_H, get_or_fetch

logger = logging.getLogger(__name__)

# ── override yfinance session with curl_cffi to avoid bot detection ─────────
try:
    from curl_cffi import requests
    import yfinance as yf

    # Override the default session creator for all new Ticker instances
    yf.Ticker._session = lambda self: requests.Session(impersonate="chrome")
    logger.info("curl_cffi session active — Yahoo bot detection bypassed")
except ImportError:
    logger.warning("curl_cffi not installed — some TSX tickers may fail with 'no timezone'")

# ── config paths ──────────────────────────────────────────────────────────────
_CONFIG_DIR = CONFIG_DIR
_WATCHLIST_PATH = WATCHLIST_YAML
_SETTINGS_PATH = SETTINGS_YAML

# ── defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_MAX_WORKERS: int = 8


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class FetchSummary:
    """
    Result of a fetch_watchlist() run.

    Attributes
    ----------
    succeeded : list[str]
        Tickers fetched and written to cache without error.
    failed    : dict[str, str]
        Ticker → error message for every ticker that raised an exception.
    total     : int
        Total number of tickers attempted.
    duration  : float
        Wall-clock time of the entire run, in seconds.
    """
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    total: int = 0
    duration: float = 0.0

    @property
    def n_succeeded(self) -> int:
        return len(self.succeeded)

    @property
    def n_failed(self) -> int:
        return len(self.failed)

    def __str__(self) -> str:
        lines = [
            f"FetchSummary | {self.n_succeeded}/{self.total} succeeded "
            f"| {self.n_failed} failed "
            f"| {self.duration:.1f}s",
        ]
        if self.failed:
            lines.append("  Failed tickers:")
            for ticker, reason in sorted(self.failed.items()):
                lines.append(f"    ✗ {ticker:<12} {reason}")
        return "\n".join(lines)


# ── public API ────────────────────────────────────────────────────────────────

def fetch_watchlist(
        watchlist_path: Path | str = _WATCHLIST_PATH,
        settings_path: Path | str = _SETTINGS_PATH,
        force: bool = False,
) -> FetchSummary:
    """
    Fetch and cache OHLCV for every ticker in the watchlist.

    Reads watchlist.yaml → tickers and settings.yaml → fetcher.max_workers,
    storage.staleness_hours, storage.cache_dir. Interval and lookback come
    from yfinance_fetcher module defaults.

    Parameters
    ----------
    watchlist_path : Path to watchlist.yaml.
    settings_path  : Path to settings.yaml.
    force          : When True, bypass staleness check and always re-fetch.

    Returns
    -------
    FetchSummary
    """
    tickers, max_workers, staleness_hours, cache_dir = _load_config(
        watchlist_path, settings_path
    )

    # Pre-bind start date and interval so the callable matches
    # the signature cache.get_or_fetch expects: fetcher(ticker) -> DataFrame.
    # End date is left to yfinance_fetcher's default (today + 1d, exclusive).
    start_str = (date.today() - timedelta(days=DEFAULT_LOOKBACK)).isoformat()
    fetcher_fn = partial(
        yf_fetchOne.fetch,
        start=start_str,
        interval=DEFAULT_INTERVAL,
    )

    summary = FetchSummary(total=len(tickers))
    t0 = time.perf_counter()

    logger.info(
        "Watchlist fetch started | tickers=%d | workers=%d "
        "| interval=%s | lookback=%dd | staleness=%dh",
        len(tickers), max_workers, DEFAULT_INTERVAL, DEFAULT_LOOKBACK, staleness_hours,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _fetch_one,
                ticker,
                fetcher_fn,
                cache_dir,
                staleness_hours,
                force,
            ): ticker
            for ticker in tickers
        }

        for future in as_completed(futures):
            ticker = futures[future]
            exc = future.exception()

            if exc is None:
                summary.succeeded.append(ticker)
                logger.info("✓ %s ready", ticker)
            else:
                summary.failed[ticker] = str(exc)
                logger.warning("✗ %s — %s", ticker, exc)

    summary.duration = time.perf_counter() - t0

    logger.info(
        "Watchlist fetch complete | %d/%d succeeded | %d failed | %.1fs",
        summary.n_succeeded, summary.total, summary.n_failed, summary.duration,
    )

    return summary


def fetch_tier_b(
        watchlist_path: Path | str = _WATCHLIST_PATH,
        settings_path: Path | str = _SETTINGS_PATH,
        force: bool = False,
) -> FetchSummary:
    """
    Fetch and cache OHLCV for tier_b universe constituents.

    Resolves ``sp500: true`` and ``tsx60: true`` markers in the tier_b
    section of watchlist.yaml into actual ticker lists, excludes any
    names already present in tier_a, and fetches OHLCV for the remainder.

    Parameters
    ----------
    watchlist_path : Path to watchlist.yaml.
    settings_path  : Path to settings.yaml.
    force          : When True, bypass staleness check and always re-fetch.

    Returns
    -------
    FetchSummary
    """
    watchlist = yaml.safe_load(Path(watchlist_path).read_text(encoding="utf-8"))
    settings = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8"))

    if "tier_b" not in watchlist:
        logger.debug("[tier_b] no tier_b section in watchlist")
        return FetchSummary()

    tier_a = set(_flatten_tier(watchlist.get("tier_a", [])))
    tier_b_entries = watchlist.get("tier_b", [])

    # Resolve tier_b markers into actual ticker lists
    all_constituents: list[str] = []
    for entry in tier_b_entries:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if not value:
                continue
            if key == "sp500":
                try:
                    from core.fetchers.sp500_constituents import get_sp500_constituents
                    all_constituents.extend(get_sp500_constituents())
                except Exception as exc:
                    logger.warning("[tier_b] sp500 resolve failed: %s", exc)
            elif key == "tsx60":
                try:
                    from core.fetchers.tsx60_constituents import get_tsx60_constituents
                    all_constituents.extend(get_tsx60_constituents())
                except Exception as exc:
                    logger.warning("[tier_b] tsx60 resolve failed: %s", exc)

    # Deduplicate and exclude tier_a
    tickers = sorted(set(all_constituents) - tier_a)

    # Also check for explicit exclude list in tier_b
    for entry in tier_b_entries:
        if isinstance(entry, dict) and "exclude" in entry:
            tickers = [t for t in tickers if t not in entry["exclude"]]

    if not tickers:
        logger.info("[tier_b] no constituents to fetch after tier_a exclusion")
        return FetchSummary()

    max_workers = settings.get("fetcher", {}).get("max_workers", _DEFAULT_MAX_WORKERS)
    staleness_hours = settings.get("storage", {}).get("staleness_hours", DEFAULT_STALENESS_H)
    cache_dir = Path(settings.get("storage", {}).get("cache_dir", "data/prices"))

    start_str = (date.today() - timedelta(days=DEFAULT_LOOKBACK)).isoformat()
    fetcher_fn = partial(
        yf_fetchOne.fetch,
        start=start_str,
        interval=DEFAULT_INTERVAL,
    )

    summary = FetchSummary(total=len(tickers))
    t0 = time.perf_counter()

    logger.info(
        "[tier_b] fetch started | tickers=%d | workers=%d",
        len(tickers), max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _fetch_one,
                ticker,
                fetcher_fn,
                cache_dir,
                staleness_hours,
                force,
            ): ticker
            for ticker in tickers
        }

        for future in as_completed(futures):
            ticker = futures[future]
            exc = future.exception()

            if exc is None:
                summary.succeeded.append(ticker)
            else:
                summary.failed[ticker] = str(exc)
                logger.warning("[tier_b] ✗ %s — %s", ticker, exc)

    summary.duration = time.perf_counter() - t0

    logger.info(
        "[tier_b] fetch complete | %d/%d succeeded | %d failed | %.1fs",
        summary.n_succeeded, summary.total, summary.n_failed, summary.duration,
    )

    return summary


# ── internals ─────────────────────────────────────────────────────────────────

def _fetch_one(
        ticker: str,
        fetcher_fn,
        cache_dir: Path,
        staleness_hours: int,
        force: bool,
) -> None:
    """
    Fetch and cache a single ticker. Exceptions propagate to the future.
    """
    # Create a fresh curl_cffi session for this thread
    from curl_cffi import requests
    session = requests.Session(impersonate="chrome")

    # Override the fetcher to include the session
    from functools import partial
    fetcher_with_session = partial(fetcher_fn, session=session)

    get_or_fetch(
        ticker=ticker,
        fetcher=fetcher_with_session,
        cache_dir=cache_dir,
        staleness_hours=staleness_hours,
        force=force,
    )


def _load_config(
        watchlist_path: Path | str,
        settings_path: Path | str,
) -> tuple[list[str], int, int, Path]:
    """
    Load and validate config from watchlist.yaml and settings.yaml.

    Supports both the legacy flat ``tickers:`` list and the two-tier
    ``tier_a:`` / ``tier_b:`` structure (Phase 2).  When ``tier_a`` is
    present, only those tickers are fetched; ``tier_b`` entries are
    ignored by the OHLCV fetcher (they are consumed later by the RP
    ranking pipeline).

    Returns
    -------
    tickers         : list[str]   Symbols to fetch (tier_a or legacy tickers).
    max_workers     : int         Thread pool size.
    staleness_hours : int         Cache freshness threshold in hours.
    cache_dir       : Path        Directory for parquet files.

    Raises
    ------
    FileNotFoundError   When either config file is missing.
    ConfigError         When the watchlist is empty.
    """
    watchlist_path = Path(watchlist_path)
    settings_path = Path(settings_path)

    if not watchlist_path.exists():
        raise FileNotFoundError(f"Watchlist config not found: {watchlist_path}")
    if not settings_path.exists():
        raise FileNotFoundError(f"Settings config not found: {settings_path}")

    watchlist = yaml.safe_load(watchlist_path.read_text(encoding="utf-8"))
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    # Two-tier structure (Phase 2): use tier_a for OHLCV fetch.
    # Legacy flat list: fall back to tickers.
    if "tier_a" in watchlist:
        tickers = _flatten_tier(watchlist["tier_a"])
    else:
        tickers = watchlist.get("tickers", [])

    if not tickers:
        raise ConfigError("tickers", reason=f"empty list in {watchlist_path}")

    max_workers = settings.get("fetcher", {}).get("max_workers", _DEFAULT_MAX_WORKERS)
    staleness_hours = settings.get("storage", {}).get("staleness_hours", DEFAULT_STALENESS_H)
    cache_dir = Path(settings.get("storage", {}).get("cache_dir", "data/prices"))

    return tickers, max_workers, staleness_hours, cache_dir


def _flatten_tier(tier: list) -> list[str]:
    """
    Flatten a tier list that may contain plain strings and dict entries
    like ``sp500: true`` into a list of ticker strings.

    Dict entries (e.g. ``sp500: true``) are filtered out — they are
    consumed by the RP ranking pipeline, not the OHLCV fetcher.
    """
    out: list[str] = []
    for entry in tier:
        if isinstance(entry, str):
            out.append(entry)
        # Dict entries like {sp500: true} are tier_b universe markers;
        # skip them in the OHLCV fetch path.
    return out
