"""
AAII (American Association of Individual Investors) sentiment survey fetcher.

Public API
──────────
``fetch_aaii() -> pd.DataFrame``
    Date-indexed weekly DataFrame with columns ``bullish``, ``neutral``,
    ``bearish`` (fractions in [0,1]) and ``spread`` (bullish - bearish).

Data source
───────────
AAII now blocks direct file downloads and simple HTML requests (403). This
fetcher simulates a real browser session by:
1. Visiting the AAII homepage to obtain session cookies.
2. Then requesting the sentiment page with the same session.
3. Parsing the HTML table.

If that fails, it falls back to cached data (operator can manually update
the cache periodically).
"""

from __future__ import annotations

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
_AAII_SENTIMENT_PAGE = f"{_AAII_BASE}/sentimentsurvey/sent_results"
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

    # ── 2. Live fetch with browser session ─────────────────────────
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
    """Use a requests.Session to get cookies from homepage, then fetch sentiment page."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": _AAII_BASE,
        "Upgrade-Insecure-Requests": "1",
    })

    try:
        # First, hit homepage to set cookies
        resp_home = session.get(_AAII_HOMEPAGE, timeout=30)
        resp_home.raise_for_status()

        # Then fetch the sentiment page
        resp_sent = session.get(_AAII_SENTIMENT_PAGE, timeout=30)
        resp_sent.raise_for_status()
    except (OSError, ValueError, requests.RequestException) as exc:
        logger.warning("[aaii] session fetch failed: %s", exc)
        return pd.DataFrame()

    return _parse_aaii_html(resp_sent.text)


def _parse_aaii_html(html: str) -> pd.DataFrame:
    """Parse the AAII sentiment-results HTML page.

    Scans every table on the page for one carrying Bullish/Bearish columns
    and a parseable date, then returns the same columns as the XLS parser
    (bullish, neutral, bearish, spread). Percentages are normalised to fractions.
    """
    try:
        import io
        tables = pd.read_html(io.StringIO(html))
    except (ValueError, ImportError) as exc:
        logger.debug("[aaii] read_html failed: %s", exc)
        return pd.DataFrame()

    for raw in tables:
        df = raw.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]
        bull = next((c for c in df.columns if c.startswith("bull")), None)
        bear = next((c for c in df.columns if c.startswith("bear")), None)
        neut = next((c for c in df.columns if c.startswith("neut")), None)
        date_c = next((c for c in df.columns if "date" in c or "week" in c), None)
        if bull is None or bear is None or date_c is None:
            continue

        rename = {date_c: "date", bull: "bullish", bear: "bearish"}
        if neut is not None:
            rename[neut] = "neutral"
        df = df.rename(columns=rename)

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date").sort_index()

        for col in ("bullish", "neutral", "bearish"):
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace("%", "", regex=False),
                    errors="coerce",
                )
        if df["bullish"].dropna().empty or df["bearish"].dropna().empty:
            continue

        # Normalise to fractions in [0,1]
        for col in ("bullish", "neutral", "bearish"):
            if col in df.columns and df[col].dropna().abs().max() > 1.5:
                df[col] = df[col] / 100.0

        df["spread"] = df["bullish"] - df["bearish"]
        keep = [c for c in ("bullish", "neutral", "bearish", "spread")
                if c in df.columns]
        out = df[keep].dropna(subset=["spread"])
        if not out.empty:
            return out

    return pd.DataFrame()


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
