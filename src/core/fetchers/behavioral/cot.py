"""
CFTC Commitments of Traders (COT) report fetcher.

Pulls weekly Disaggregated COT data from CFTC's public Socrata JSON API
(no API key required). One file per contract under
``data/behavioral/cot_<short>.parquet``.

Public API
──────────
``fetch_cot(contract, ...) -> pd.DataFrame``
    Per-contract DataFrame keyed by report-date. Required column for the
    behavioral classifier: ``mm_net`` (managed-money long minus short).

``fetch_all_cot(...) -> dict[str, pd.DataFrame]``
    Returns a dict ``{<short>: df}`` for every contract in
    ``_COMMODITY_CODES``. The behavioral classifier reads ``"cot_es"``
    from the bundled ``data`` dict, so the loader must map ``"es"`` →
    ``"cot_es"`` when assembling its payload.

Data source
───────────
``https://publicreporting.cftc.gov/resource/72hh-3qaq.json`` — disaggregated
weekly futures-and-options reports. Filter by ``contract_market_name``
plus the formal exchange string to disambiguate variants.

Failure modes
─────────────
- Network failure / 5xx / 429 → load cached parquet, else empty DataFrame.
- Schema drift on Socrata → return what parsed; missing ``mm_net`` is OK
  (downstream classifier treats axis as missing).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.fetchers.http import request_with_retry
from core.paths import BEHAVIORAL_DIR

logger = logging.getLogger(__name__)

# CFTC Socrata resource ID. ⚠ UNRESOLVED as of 2026-05-31 — see notes:
#   - 72hh-3qaq (original) → 404 in production log.
#   - s9da-n2w9 (tried by a parallel edit) → also 404 in production log.
#   - 72hh-3qpy (Disaggregated Futures-Only) → set here but NOT live-verified
#     (the sandbox could not reach CFTC to confirm).
# DEEPER MISMATCH to resolve before trusting COT data: the contracts in
# _COMMODITY_CODES (E-MINI S&P 500, UST 10Y, VIX) are *financial* futures,
# which live in the **Traders in Financial Futures (TFF)** report, NOT the
# Disaggregated report (that one covers physical commodities). TFF also uses
# different position columns — `lev_money_positions_long_all/_short_all`
# (leveraged funds) rather than the `m_money_positions_*` (managed money) that
# _normalise_cot_rows looks for. So fixing COT properly means: (1) point at the
# correct TFF Futures-Only resource ID from
# https://publicreporting.cftc.gov/api-docs/, AND (2) teach _normalise_cot_rows
# to read the lev_money_* columns. Until then COT fails open and is excluded
# from the score (behavioral confidence simply drops — no garbage ingested).
_COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
_DATA_DIR = BEHAVIORAL_DIR
_DEFAULT_STALENESS_DAYS = 7

# (short_name, official CFTC contract_market_name) — formal names are
# critical here: the Socrata API uses exact-match filters, and even a
# trailing whitespace or hyphen variant produces an empty result set.
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
    pd.DataFrame indexed by report-date with at least ``mm_net``,
        ``mm_long``, ``mm_short``. Empty DataFrame if unresolvable.
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
    if not force and _cache_fresh(meta_path, staleness_days):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[cot] %s loaded from cache (%d rows)",
                         contract, len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[cot] cache read failed for %s: %s",
                           contract, exc, exc_info=True)

    # ── 2. live fetch via Socrata ────────────────────────────────────────
    # 260 records ≈ 5 years of weekly data. CFTC's contract_market_name
    # field is the human-readable label; we match by SoQL substring to
    # tolerate small whitespace differences between weekly releases.
    params = {
        "$where": f"upper(contract_market_name) like upper('%{contract_name}%')",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "260",
    }
    try:
        resp = request_with_retry(
            "GET", _COT_URL,
            params=params, timeout=20,
            rate_limit_key="cftc", rate_limit_interval_s=1.0,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (OSError, ValueError, RuntimeError) as exc:
        # Expected when CFTC rotates the Socrata resource ID (404) or rate-
        # limits us. Fail open to cache/empty so the score excludes COT rather
        # than ingesting nothing-or-garbage. No stack trace for this path.
        logger.warning("[cot] fetch failed for %s: %s — excluding from score "
                       "(check resource ID if 404)", contract, exc)
        return _load_cached_or_empty(parquet_path)

    df = _normalise_cot_rows(rows)
    if df.empty:
        logger.warning("[cot] %s: empty result from CFTC", contract)
        return _load_cached_or_empty(parquet_path)

    # ── 3. cache write ───────────────────────────────────────────────────
    try:
        df.to_parquet(parquet_path)
        _write_meta(meta_path)
        logger.info("[cot] %s fetched and cached (%d rows)",
                    contract, len(df))
    except (OSError, ValueError) as exc:
        logger.warning("[cot] cache write failed for %s: %s",
                       contract, exc, exc_info=True)

    return df


# ── helpers ──────────────────────────────────────────────────────────────────


def _normalise_cot_rows(rows: list[dict]) -> pd.DataFrame:
    """Convert raw Socrata records into a typed, date-indexed DataFrame.

    Socrata returns strings for every field. We coerce the date and the
    three managed-money positions to numeric, derive ``mm_net``, and
    discard rows where the date can't be parsed.
    """
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # The disaggregated dataset uses different column names depending on
    # which API version is hit. Cover both.
    date_col = None
    for candidate in ("report_date_as_yyyy_mm_dd", "yyyy_report_week_ww"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col is None:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()

    long_col = next((c for c in ("m_money_positions_long",
                                 "m_money_positions_long_all")
                     if c in df.columns), None)
    short_col = next((c for c in ("m_money_positions_short",
                                  "m_money_positions_short_all")
                      if c in df.columns), None)
    if long_col is None or short_col is None:
        # Schema drift — return what we have so the cache at least
        # remembers the dates. Downstream classifier sees empty mm_net
        # and treats positioning as missing.
        return df

    df["mm_long"] = pd.to_numeric(df[long_col], errors="coerce")
    df["mm_short"] = pd.to_numeric(df[short_col], errors="coerce")
    df["mm_net"] = df["mm_long"] - df["mm_short"]

    return df[["mm_long", "mm_short", "mm_net"]].dropna(subset=["mm_net"])


def _cache_fresh(meta_path: Path, staleness_days: int) -> bool:
    if not meta_path.exists():
        return False
    try:
        mtime = meta_path.stat().st_mtime
        age_days = (datetime.now().timestamp() - mtime) / 86400
        return age_days < staleness_days
    except (OSError, ValueError) as exc:
        logger.debug("[cot] cache freshness check failed: %s", exc)
        return False


def _write_meta(meta_path: Path) -> None:
    meta = {"fetched_at": datetime.now().isoformat()}
    try:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("[cot] meta write failed at %s: %s", meta_path, exc)


def _load_cached_or_empty(parquet_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except (OSError, ValueError) as exc:
            logger.debug("[cot] cached parquet read failed at %s: %s",
                         parquet_path, exc)
    return pd.DataFrame()
