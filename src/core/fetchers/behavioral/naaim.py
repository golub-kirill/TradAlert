"""
NAAIM (National Association of Active Investment Managers) Exposure Index fetcher.

Public API
──────────
``fetch_naaim() -> pd.DataFrame``
    Date-indexed weekly DataFrame with an ``exposure`` column ∈ [0, 200]
    (managers can be net-long >100% or net-short, so the range exceeds
    100). The behavioral classifier uses this for positioning extremes.

Data source
───────────
NAAIM no longer provides a public Excel file of the full history. The latest
exposure index is published on their website. This fetcher:
1. Loads cached historical data from ``data/behavioral/naaim.parquet``.
2. Fetches the latest value from the NAAIM website.
3. Appends the new value if its date is newer than the last cached row.
4. Saves the updated cache.

If the website is unreachable, returns the cached data (stale but usable).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core.fetchers import cache_meta
from core.fetchers.http import request_with_retry
from core.paths import BEHAVIORAL_DIR

logger = logging.getLogger(__name__)

_NAAIM_CURRENT_URL = "https://www.naaim.org/resources/naaim-exposure-index/"
_DATA_DIR = BEHAVIORAL_DIR
_DEFAULT_STALENESS_DAYS = 7  # weekly release


def fetch_naaim(
        data_dir: Path | str | None = None,
        staleness_days: int = _DEFAULT_STALENESS_DAYS,
        force: bool = False,
) -> pd.DataFrame:
    """Fetch the NAAIM exposure index (historical + latest)."""
    data_dir_p = Path(data_dir) if data_dir else _DATA_DIR
    data_dir_p.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir_p / "naaim.parquet"
    meta_path = data_dir_p / "naaim.meta.json"

    # 1. Load cached history
    cached_df = _load_cached_or_empty(parquet_path)
    cache_fresh = cache_meta.is_fresh(meta_path, staleness_days * 86400)

    if not force and cache_fresh and not cached_df.empty:
        logger.debug("[naaim] loaded from cache (%d rows)", len(cached_df))
        return cached_df

    # 2. Fetch the latest index value from the website
    latest_exposure, latest_date = _fetch_latest_naaim()
    if latest_exposure is None:
        age = cache_meta.age_seconds(parquet_path)
        if age is not None and age > staleness_days * 86400:
            logger.warning(
                "[naaim] could not fetch current value — serving STALE cache "
                "(%.1f d old, > %g d window); value may be outdated.",
                age / 86400.0, staleness_days,
            )
        else:
            logger.warning("[naaim] could not fetch current value — returning cached data")
        return cached_df if not cached_df.empty else pd.DataFrame()

    # 3. Append to cache if newer
    new_row = pd.DataFrame({"exposure": [latest_exposure]}, index=[latest_date])
    if cached_df.empty:
        updated_df = new_row
    elif latest_date > cached_df.index[-1]:
        updated_df = pd.concat([cached_df, new_row])
    else:
        # Already up to date
        updated_df = cached_df

    # 4. Write back
    try:
        updated_df.to_parquet(parquet_path)
        cache_meta.write_meta(meta_path)
        logger.info("[naaim] updated cache (%d rows)", len(updated_df))
    except (OSError, ValueError) as exc:
        logger.warning("[naaim] cache write failed: %s", exc)

    return updated_df


def _fetch_latest_naaim() -> tuple[float | None, pd.Timestamp | None]:
    """
    Scrape the latest exposure index from the NAAIM website.
    Returns (exposure_value, date_of_survey). Date is approximated as the most
    recent Wednesday (NAAIM releases on Wednesdays).
    """
    try:
        resp = request_with_retry(
            "GET", _NAAIM_CURRENT_URL, timeout=30,
            rate_limit_key="naaim", rate_limit_interval_s=1.0,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("[naaim] fetch failed: %s", exc)
        return None, None

    # Match the NAAIM exposure value only when LABELLED — no bare "NN%" fallback
    # (it grabs unrelated page percentages); a failed parse returns None (fail-open).
    # The 2026-06 page phrases the current reading "...Exposure Index number is*: 92.83"
    # (the older bare "Exposure Index: NN" forms are kept as fallbacks).
    # ⚠ SUNSET: NAAIM announced the FREE feed transitions to subscription-only on
    # 2026-08-01 — this scrape WILL break then. Plan to drop NAAIM at that point
    # (positioning falls back to COT-only, as AAII was dropped when its free feed gated).
    # Strip HTML tags + collapse whitespace so the label and value are contiguous —
    # the live page renders the number in a separate inline element, so a regex on
    # raw HTML (tags between "is*:" and "92.83") never matches.
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", resp.text))
    patterns = [
        r"Exposure\s*Index\s*number\s*is[\*:\s]*(\d{1,3}(?:\.\d+)?)",
        r"Exposure\s*Index[:\s]*(\d{1,3}(?:\.\d+)?)",
        r"Current\s*Exposure[:\s]*(\d{1,3}(?:\.\d+)?)",
        r"NAAIM\s*Number[:\s]*(\d{1,3}(?:\.\d+)?)",
    ]
    value = None
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            break
    if value is None:
        logger.warning("[naaim] could not parse exposure value from HTML")
        return None, None

    # Sanity-bound the scrape: the NAAIM Exposure Index runs roughly 0–200 (the
    # regex only captures positives), so a value outside ~[0, 250] is almost
    # certainly an unrelated number lifted off the page — discard it rather than
    # cache a bogus reading that a later backtest would read back from parquet.
    if not (0.0 <= value <= 250.0):
        logger.warning(
            "[naaim] parsed exposure %.1f outside sane range [0, 250] — likely a "
            "mis-scrape; discarding.", value)
        return None, None

    # Survey date: use last Wednesday (if today is Wednesday, use today)
    today = datetime.today()
    days_back = (today.weekday() - 2) % 7  # Wednesday = 2
    survey_date = today - timedelta(days=days_back)
    return value, pd.Timestamp(survey_date.date())

def _load_cached_or_empty(parquet_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            if not df.empty and "exposure" in df.columns:
                return df
        except (OSError, ValueError) as exc:
            logger.debug("[naaim] cached parquet read failed: %s", exc)
    return pd.DataFrame()
