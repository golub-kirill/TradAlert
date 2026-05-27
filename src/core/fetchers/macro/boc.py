"""
Bank of Canada Valet REST client.

No API key required. Base URL:
 https://www.bankofcanada.ca/valet/observations/{series_id}/json

Series IDs:
 V39079             — BoC overnight rate target
 BD.CDN.2YR.DQ.YLD  — Government of Canada 2-year benchmark bond yield
 BD.CDN.5YR.DQ.YLD  — Government of Canada 5-year benchmark bond yield
 BD.CDN.10YR.DQ.YLD — Government of Canada 10-year benchmark bond yield

Note: the legacy V39055/56/57 bond-yield IDs were retired by the Bank of
Canada and now 404 on the Valet API. The current benchmark yields live in
the "bond_yields_benchmark" group under the BD.CDN.*.DQ.YLD names. The
overnight-rate series (V39079) is unaffected and still valid.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from core.paths import MACRO_DIR

import pandas as pd
import requests

from core.fetchers.http import request_with_retry

logger = logging.getLogger(__name__)

_BOC_BASE_URL = "https://www.bankofcanada.ca/valet/observations"
_DEFAULT_STALENESS_HOURS = 24


def fetch_boc_series(
        series_id: str,
        series_dir: Path | str = MACRO_DIR,
        staleness_hours: int = _DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> pd.DataFrame:
    """
    Fetch a single BoC Valet series, cached as parquet.

    Parameters
    ----------
    series_id : BoC series ID (e.g. ``"V39079"``).
    series_dir : Directory for parquet files.
    staleness_hours : Re-fetch if cache is older than this.
    force : Always re-fetch, ignoring cache.

    Returns
    -------
    DataFrame with DatetimeIndex and ``value`` column.
    Empty DataFrame on failure (fail-open).
    """
    series_dir = Path(series_dir)

    series_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = series_dir / f"{series_id}.parquet"
    meta_path = series_dir / f"{series_id}.meta.json"

    if not force and _cache_fresh(meta_path, staleness_hours):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[boc] %s loaded from cache (%d rows)", series_id, len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[boc] cache read failed for %s: %s", series_id, exc, exc_info=True)

    url = f"{_BOC_BASE_URL}/{series_id}/json"
    params = {"start_date": "2000-01-01"}

    try:
        resp = request_with_retry('GET', url, params=params, timeout=30, retries=3, backoff=1.0,
                                  rate_limit_key='boc-valet', rate_limit_interval_s=0.51, headers={
                "User-Agent": "TradAlert/1.0 (tradalert@example.com)",
                "Accept": "application/json",
            })
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        # A 404 means the Valet series ID is retired/deprecated. That is an
        # expected, recoverable condition — log a single clean line (NO stack
        # trace) and fail open. Other HTTP statuses are unexpected but still
        # non-fatal: skip this series rather than poison the regime calc.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            logger.warning(
                "[boc] series %s returned HTTP 404 (retired/deprecated ID) — "
                "skipping; no data fed to calculations", series_id,
            )
        else:
            logger.warning(
                "[boc] series %s returned HTTP %s — skipping (fail-open)",
                series_id, status,
            )
        return _load_cached_or_empty(parquet_path)
    except (OSError, ValueError, RuntimeError) as exc:
        # Network failure (OSError), bad JSON (ValueError), or other runtime
        # error. Non-fatal: skip the series and fall back to cache/empty so a
        # single broken feed never pollutes the regime calculation. No stack
        # trace — this path is expected to fire on transient outages.
        logger.warning("[boc] fetch failed for %s: %s — skipping (fail-open)", series_id, exc)
        return _load_cached_or_empty(parquet_path)

    # BoC returns nested structure: {"observations": [{"d": "2024-01-01", "V39079": {"v": "5.0"}}]}
    observations = data.get("observations", [])
    if not observations:
        logger.warning("[boc] no observations returned for %s", series_id)
        return _load_cached_or_empty(parquet_path)

    df = _parse_boc_observations(observations, series_id)
    if df.empty:
        return _load_cached_or_empty(parquet_path)

    try:
        df.to_parquet(parquet_path)
        _write_meta(meta_path)
        logger.info("[boc] %s fetched and cached (%d rows)", series_id, len(df))
    except (OSError, ValueError) as exc:
        logger.warning("[boc] cache write failed for %s: %s", series_id, exc, exc_info=True)

    return df


def _parse_boc_observations(observations: list[dict], series_id: str) -> pd.DataFrame:
    """Parse BoC Valet JSON observations into a DataFrame."""
    records = []
    for obs in observations:
        date_str = obs.get("d")
        if date_str is None:
            continue
        # BoC nests the value under the series ID key: {"d": "...", "V39079": {"v": "5.0"}}
        series_data = obs.get(series_id, {})
        if isinstance(series_data, dict):
            value = series_data.get("v")
        else:
            value = series_data
        if value is None:
            continue
        try:
            dt = pd.Timestamp(date_str)
            val = float(value)
            records.append((dt, val))
        except (ValueError, TypeError):
            continue

    if not records:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=["value"])

    df = pd.DataFrame(records, columns=["date", "value"])
    df = df.set_index("date")
    df.index.name = None
    df = df.sort_index()
    return df


def _cache_fresh(meta_path: Path, staleness_hours: int) -> bool:
    if not meta_path.exists():
        return False
    try:
        mtime = meta_path.stat().st_mtime
        age_hours = (datetime.now().timestamp() - mtime) / 3600
        return age_hours < staleness_hours
    except (OSError, ValueError) as exc:
        logger.debug("[boc] cache freshness check failed: %s", exc)
        return False


def _write_meta(meta_path: Path) -> None:
    import json
    meta = {"fetched_at": datetime.now().isoformat()}
    meta_path.write_text(json.dumps(meta))


def _load_cached_or_empty(parquet_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except (OSError, ValueError) as exc:
            logger.debug("[boc] cached parquet read failed at %s: %s", parquet_path, exc)
    return pd.DataFrame(index=pd.DatetimeIndex([]), columns=["value"])
