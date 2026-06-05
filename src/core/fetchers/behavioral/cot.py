"""
CFTC Commitments of Traders (COT) report fetcher – TFF (Traders in Financial Futures).

Pulls weekly TFF Futures‑Only data from CFTC's public Socrata JSON API
(no API key required). One file per contract under
``data/behavioral/cot_<short>.parquet``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from core.fetchers import cache_meta
from core.fetchers.http import request_with_retry
from core.paths import BEHAVIORAL_DIR

logger = logging.getLogger(__name__)

# CFTC Socrata resource ID for Traders in Financial Futures (TFF) Futures-Only
# https://publicreporting.cftc.gov/Commitments-of-Traders/TFF-Futures-Only/gpe5-46if/about_data
_TFF_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_DATA_DIR = BEHAVIORAL_DIR
_DEFAULT_STALENESS_DAYS = 7

# (short_name, official CFTC contract_market_name as it appears in TFF)
# Substring matching is used, so the exact string can be a human‑readable
# fragment – but using the full official name is most reliable.
_COMMODITY_CODES: dict[str, str] = {
    "es": "E-MINI S&P 500",
    "tnote": "UST 10Y NOTE",
    "vix": "VIX FUTURES",
}


def fetch_all_cot(
        data_dir: Path | str | None = None,
        staleness_days: int = _DEFAULT_STALENESS_DAYS,
        force: bool = False,
) -> dict[str, pd.DataFrame]:
    """Fetch COT data for every contract in ``_COMMODITY_CODES``.

    Returns a dict keyed by the short name (e.g. ``"es"``). When the
    network is unreachable, returns whatever loaded from cache (possibly
    an empty dict). Never raises.
    """
    out: dict[str, pd.DataFrame] = {}
    for short, contract_name in _COMMODITY_CODES.items():
        try:
            df = fetch_cot(short, data_dir=data_dir,
                           staleness_days=staleness_days, force=force)
            if not df.empty:
                out[short] = df
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("[cot] %s fetch failed: %s", short, exc)
    return out


def fetch_cot(
        contract: str,
        data_dir: Path | str | None = None,
        staleness_days: int = _DEFAULT_STALENESS_DAYS,
        force: bool = False,
) -> pd.DataFrame:
    """Fetch one contract's COT history.

    Parameters
    ----------
    contract : Short key from ``_COMMODITY_CODES`` (e.g. ``"es"``).
    data_dir : Override the default ``data/behavioral`` directory.
    staleness_days : Window inside which the cache is considered fresh.
        COT is published weekly so the default 7 days is appropriate.
    force : Bypass cache freshness; always re-fetch.

    Returns
    -------
    pd.DataFrame indexed by report-date with at least ``lev_net``,
        ``lev_long``, ``lev_short``. Empty DataFrame if unresolvable.
    """
    contract = contract.lower()
    if contract not in _COMMODITY_CODES:
        logger.warning("[cot] unknown contract %r (known: %s)",
                       contract, list(_COMMODITY_CODES))
        return pd.DataFrame()

    contract_name = _COMMODITY_CODES[contract]
    data_dir_p = Path(data_dir) if data_dir else _DATA_DIR
    data_dir_p.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir_p / f"cot_{contract}.parquet"
    meta_path = data_dir_p / f"cot_{contract}.meta.json"

    # ── 1. fresh-cache short-circuit ─────────────────────────────────────
    if not force and cache_meta.is_fresh(meta_path, staleness_days * 86400):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[cot] %s loaded from cache (%d rows)",
                         contract, len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[cot] cache read failed for %s: %s",
                           contract, exc, exc_info=True)

    # ── 2. live fetch via Socrata ────────────────────────────────────────
    # 260 records ≈ 5 years of weekly data. Use substring matching on
    # contract_market_name to tolerate whitespace or minor differences.
    params = {
        "$where": f"upper(contract_market_name) like upper('%{contract_name}%')",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "260",
    }
    try:
        resp = request_with_retry(
            "GET", _TFF_URL,
            params=params, timeout=20,
            rate_limit_key="cftc", rate_limit_interval_s=1.0,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("[cot] fetch failed for %s: %s — excluding from score "
                       "(check TFF resource ID if 404)", contract, exc)
        return _load_cached_or_empty(parquet_path)

    df = _normalise_tff_rows(rows)
    if df.empty:
        logger.warning("[cot] %s: empty result from CFTC TFF endpoint", contract)
        return _load_cached_or_empty(parquet_path)

    # ── 3. cache write ───────────────────────────────────────────────────
    try:
        df.to_parquet(parquet_path)
        cache_meta.write_meta(meta_path)
        logger.info("[cot] %s fetched and cached (%d rows)",
                    contract, len(df))
    except (OSError, ValueError) as exc:
        logger.warning("[cot] cache write failed for %s: %s",
                       contract, exc, exc_info=True)

    return df


# ── helpers ──────────────────────────────────────────────────────────────────


def _normalise_tff_rows(rows: list[dict]) -> pd.DataFrame:
    """Convert raw TFF Socrata records into a typed, date-indexed DataFrame.

    Socrata returns strings for every field. We coerce the date and the
    three leveraged‑fund positions to numeric, derive ``lev_net``, and
    discard rows where the date can't be parsed.
    """
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Date column – TFF uses report_date_as_yyyy_mm_dd
    date_col = None
    for candidate in ("report_date_as_yyyy_mm_dd", "yyyy_report_week_ww"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()

    # Leveraged funds columns (TFF specific)
    long_col = next((c for c in ("lev_money_positions_long_all",
                                 "lev_money_positions_long")
                     if c in df.columns), None)
    short_col = next((c for c in ("lev_money_positions_short_all",
                                  "lev_money_positions_short")
                      if c in df.columns), None)

    if long_col is None or short_col is None:
        # Schema drift – return what we have so the cache at least
        # remembers the dates. Downstream classifier sees empty lev_net
        # and treats positioning as missing.
        logger.warning("[cot] TFF columns missing: long=%s, short=%s",
                       long_col, short_col)
        return df

    df["lev_long"] = pd.to_numeric(df[long_col], errors="coerce")
    df["lev_short"] = pd.to_numeric(df[short_col], errors="coerce")
    df["lev_net"] = df["lev_long"] - df["lev_short"]

    return df[["lev_long", "lev_short", "lev_net"]].dropna(subset=["lev_net"])


def _load_cached_or_empty(parquet_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except (OSError, ValueError) as exc:
            logger.debug("[cot] cached parquet read failed at %s: %s",
                         parquet_path, exc)
    return pd.DataFrame()