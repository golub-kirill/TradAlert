"""
FRED (Federal Reserve Economic Data) REST client.

Uses the FRED API key from the ``FRED_API_KEY`` environment variable
(typically set in ``config/secrets.env``).

Base URL: https://api.stlouisfed.org/fred/series/observations

Rate limit: 120 req/min unauthenticated, 1000 req/min with key.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
import requests

from core.fetchers import cache_meta
from core.fetchers.http import request_with_retry
from core.paths import MACRO_DIR

logger = logging.getLogger(__name__)

_FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
_DEFAULT_STALENESS_HOURS = 24


def fetch_fred_series(
        series_id: str,
        series_dir: Path | str = MACRO_DIR,
        staleness_hours: int = _DEFAULT_STALENESS_HOURS,
        force: bool = False,
) -> pd.DataFrame:
    """
    Fetch a single FRED series, cached as parquet.

    Parameters
    ----------
    series_id : FRED series ID (e.g. ``"FEDFUNDS"``).
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

    if not force and cache_meta.is_fresh(meta_path, staleness_hours * 3600):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[fred] %s loaded from cache (%d rows)", series_id, len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[fred] cache read failed for %s: %s", series_id, exc, exc_info=True)

    api_key = _get_api_key()
    if api_key is None:
        logger.warning("[fred] no API key configured; returning cached or empty for %s", series_id)
        return _load_cached_or_empty(parquet_path, staleness_hours)

    url = _FRED_BASE_URL
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }

    try:
        resp = request_with_retry('GET', url, params=params, timeout=30, retries=3, backoff=1.0,
                                  rate_limit_key='api.stlouisfed.org', rate_limit_interval_s=0.51)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        # do NOT log str(exc) — requests embeds the full URL
        # (with api_key=...) in HTTPError messages. Log type + status only.
        status = getattr(getattr(exc, "response", None), "status_code", "n/a")
        logger.warning(
            "[fred] fetch failed for %s: %s (status=%s)",
            series_id, type(exc).__name__, status,
        )
        return _load_cached_or_empty(parquet_path, staleness_hours)
    except (KeyError, ValueError, TypeError) as exc:
        # Defensive: parsing/JSON errors after a 200 response can't carry the URL.
        logger.warning("[fred] response parse failed for %s: %s",
                       series_id, type(exc).__name__, exc_info=True)
        return _load_cached_or_empty(parquet_path, staleness_hours)

    observations = data.get("observations", [])
    if not observations:
        logger.warning("[fred] no observations returned for %s", series_id)
        return _load_cached_or_empty(parquet_path, staleness_hours)

    df = _parse_fred_observations(observations, series_id)
    if df.empty:
        return _load_cached_or_empty(parquet_path, staleness_hours)

    try:
        df.to_parquet(parquet_path)
        cache_meta.write_meta(meta_path)
        logger.info("[fred] %s fetched and cached (%d rows)", series_id, len(df))
    except (OSError, ValueError) as exc:
        logger.warning("[fred] cache write failed for %s: %s", series_id, exc, exc_info=True)

    return df


def _parse_fred_observations(
        observations: list[dict],
        series_id: str,
) -> pd.DataFrame:
    """Parse FRED JSON observations into a DataFrame."""
    records = []
    for obs in observations:
        date_str = obs.get("date")
        value_str = obs.get("value", ".")
        if date_str is None or value_str == ".":
            continue
        try:
            dt = pd.Timestamp(date_str)
            val = float(value_str)
            records.append((dt, val))
        except (ValueError, TypeError):
            continue

    if not records:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=["value"])

    df = pd.DataFrame(records, columns=["date", "value"])
    df = df.set_index("date")
    df.index.name = None
    df = df.sort_index()

    # Monthly series get a release_date column
    if _is_monthly_series(series_id):
        df["release_date"] = df.index

    return df


def _is_monthly_series(series_id: str) -> bool:
    """Monthly FRED series that need release_date tracking."""
    monthly = {"CPIAUCSL", "PCEPILFE", "CPILFESL", "CPIAUCNS"}
    return series_id in monthly


def _get_api_key() -> str | None:
    """Read the FRED API key from the env var named in settings.yaml.

    previously the env-var name was hardcoded as "FRED_API_KEY".
    Now it's read from ``settings.yaml::macro.fred_api_key_env`` (falling
    back to "FRED_API_KEY" so existing deployments keep working).
    """
    from core.defaults import DEFAULTS
    import yaml
    from pathlib import Path
    env_name = DEFAULTS.get("settings.macro.fred_api_key_env")
    settings_path = Path(__file__).resolve().parent.parent.parent.parent.parent / "config" / "settings.yaml"
    try:
        if settings_path.exists():
            cfg = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
            env_name = (cfg.get("macro", {}) or {}).get("fred_api_key_env", env_name)
    except (OSError, AttributeError) as exc:
        # YAML scanner errors surface as yaml.YAMLError (subclass of Exception).
        # Keep settings.yaml read tolerant — fall through to default name.
        logger.debug("[fred] reading settings.yaml failed (%s); using default env name", exc)
    return os.environ.get(env_name)

def _load_cached_or_empty(parquet_path: Path,
                          staleness_hours: float = _DEFAULT_STALENESS_HOURS) -> pd.DataFrame:
    """Load cached parquet (fail-open) when a fetch fails, but WARN with the cache
    age when it is past the staleness window so an unbounded-stale cache can't
    masquerade as a fresh series (audit F2)."""
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            age = cache_meta.age_seconds(parquet_path)
            if age is not None and age > staleness_hours * 3600:
                logger.warning(
                    "[fred] serving STALE cache for %s — %.1f h old (> %g h window); "
                    "upstream fetch failed, value may be outdated.",
                    parquet_path.stem, age / 3600.0, staleness_hours,
                )
            return df
        except (OSError, ValueError) as exc:
            logger.debug("[fred] cached parquet read failed at %s: %s", parquet_path, exc)
    return pd.DataFrame(index=pd.DatetimeIndex([]), columns=["value"])
