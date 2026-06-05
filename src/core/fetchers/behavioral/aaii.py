"""
AAII (American Association of Individual Investors) sentiment survey fetcher.

Public API
──────────
``fetch_aaii() -> pd.DataFrame``
    Date-indexed weekly DataFrame with columns ``bullish``, ``neutral``,
    ``bearish`` (fractions in [0,1]) and ``spread`` (bullish - bearish).
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from core.paths import BEHAVIORAL_DIR

logger = logging.getLogger(__name__)

_AAII_BASE = "https://www.aaii.com"
_AAII_HOMEPAGE = f"{_AAII_BASE}/"
_AAII_SENTIMENT_XLS = f"{_AAII_BASE}/files/surveys/sentiment.xls"
_DATA_DIR = BEHAVIORAL_DIR
_DEFAULT_STALENESS_DAYS = 7  # survey is weekly


def fetch_aaii(
        data_dir: Path | str | None = None,
        staleness_days: int = _DEFAULT_STALENESS_DAYS,
        force: bool = False,
) -> pd.DataFrame:
    """Fetch the AAII bull-bear survey.

    Returns a DataFrame indexed by survey date with columns
    ``bullish``, ``neutral``, ``bearish``, ``spread``.

    All failures fail-open to cached / empty data.
    """
    data_dir_p = Path(data_dir) if data_dir else _DATA_DIR
    data_dir_p.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir_p / "aaii.parquet"
    meta_path = data_dir_p / "aaii.meta.json"

    # ── 1. Cache short-circuit ─────────────────────────────────────
    if not force and _cache_fresh(meta_path, staleness_days):
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug("[aaii] loaded from cache (%d rows)", len(df))
            return df
        except (OSError, ValueError) as exc:
            logger.warning("[aaii] cache read failed: %s", exc, exc_info=True)

    # ── 2. Live fetch with browser-like session ────────────────────
    df = _fetch_aaii_with_session()

    if df.empty:
        logger.warning("[aaii] all sources unavailable — excluding from score")
        return _load_cached_or_empty(parquet_path)

    # ── 3. Cache write ─────────────────────────────────────────────
    try:
        df.to_parquet(parquet_path)
        _write_meta(meta_path)
        logger.info("[aaii] fetched and cached (%d rows)", len(df))
    except (OSError, ValueError) as exc:
        logger.warning("[aaii] cache write failed: %s", exc, exc_info=True)

    return df


def _fetch_aaii_with_session() -> pd.DataFrame:
    """Use a requests.Session to obtain cookies from the homepage, then download the Excel file."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    })

    try:
        # 1. Visit homepage to get cookies
        resp_home = session.get(_AAII_HOMEPAGE, timeout=30)
        resp_home.raise_for_status()

        # 2. Download the Excel file
        resp_xls = session.get(_AAII_SENTIMENT_XLS, timeout=30)
        resp_xls.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("[aaii] session fetch failed: %s", exc)
        return pd.DataFrame()

    # 3. Parse the Excel content
    return _parse_sentiment_xls(resp_xls.content)


def _parse_sentiment_xls(content: bytes) -> pd.DataFrame:
    """Parse the SENTIMENT sheet from the Excel file, returning a clean DataFrame."""
    try:
        xls_data = io.BytesIO(content)
        # Read without assuming header; we'll locate it dynamically
        df_raw = pd.read_excel(xls_data, sheet_name="SENTIMENT", header=None)
    except Exception as exc:
        logger.warning("[aaii] Excel parsing failed: %s", exc)
        return pd.DataFrame()

    # Locate the row that contains the column headers (must have 'date' and 'bullish')
    header_row_idx = None
    for idx, row in df_raw.iterrows():
        # Convert each cell to string, handle NaNs
        row_str = row.astype(str).str.lower()
        if row_str.str.contains('date').any() and row_str.str.contains('bullish').any():
            header_row_idx = idx
            break

    if header_row_idx is None:
        logger.warning("[aaii] Could not find header row in SENTIMENT sheet")
        return pd.DataFrame()

    # Set headers and drop the header row
    df = df_raw.iloc[header_row_idx:].copy()
    # Force all column names to strings
    raw_columns = df.iloc[0].astype(str).str.strip()
    df.columns = raw_columns
    df = df[1:].reset_index(drop=True)

    # Rename columns to canonical names (case-insensitive)
    rename_map = {}
    for col in df.columns:
        # Skip if column is not a string (e.g., NaN)
        if not isinstance(col, str):
            continue
        col_lower = col.lower()
        if 'date' in col_lower:
            rename_map[col] = 'date'
        elif col_lower == 'bullish':
            rename_map[col] = 'bullish'
        elif col_lower == 'neutral':
            rename_map[col] = 'neutral'
        elif col_lower == 'bearish':
            rename_map[col] = 'bearish'
    df = df.rename(columns=rename_map)

    required = {'date', 'bullish', 'neutral', 'bearish'}
    if not required.issubset(df.columns):
        logger.warning("[aaii] Missing required columns: %s", required - set(df.columns))
        return pd.DataFrame()

    # Type conversion
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    for col in ['bullish', 'neutral', 'bearish']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Drop rows with missing essential data
    df = df.dropna(subset=['date', 'bullish', 'neutral', 'bearish'])

    # Normalise percentages to fractions if necessary
    for col in ['bullish', 'neutral', 'bearish']:
        if not df[col].empty and df[col].abs().max() > 1.5:
            df[col] = df[col] / 100.0

    # Compute spread
    df['spread'] = df['bullish'] - df['bearish']

    # Final formatting
    df = df.set_index('date').sort_index()
    out = df[['bullish', 'neutral', 'bearish', 'spread']].copy()

    if out.empty:
        logger.warning("[aaii] No valid data after parsing")
    else:
        logger.info("[aaii] Successfully parsed %d weekly observations", len(out))

    return out


def _cache_fresh(meta_path: Path, staleness_days: int) -> bool:
    if not meta_path.exists():
        return False
    try:
        mtime = meta_path.stat().st_mtime
        age_days = (datetime.now().timestamp() - mtime) / 86400
        return age_days < staleness_days
    except (OSError, ValueError) as exc:
        logger.debug("[aaii] cache freshness check failed: %s", exc)
        return False


def _write_meta(meta_path: Path) -> None:
    meta = {"fetched_at": datetime.now().isoformat()}
    try:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("[aaii] meta write failed at %s: %s", meta_path, exc)


def _load_cached_or_empty(parquet_path: Path) -> pd.DataFrame:
    if parquet_path.exists():
        try:
            return pd.read_parquet(parquet_path)
        except (OSError, ValueError) as exc:
            logger.debug("[aaii] cached parquet read failed at %s: %s",
                         parquet_path, exc)
    return pd.DataFrame()