"""
TradingView economic-calendar fetcher — FOMC / CPI / NFP event dates (US/CA).

A live, auto-updating replacement for the hard-coded 2026 list (which goes dark in
2027): the TradingView calendar API carries history back to ~2015 AND extends forward
as the schedule is published. Undocumented JSON endpoint, so — like every fetcher
here — it is cached to disk and fail-open (network failure → serve cache, never abort).

NO LOOK-AHEAD: only event DATES are used (the entry-day no-trade gate + the advisory
``event_risk`` flag), never the actual/forecast values. FOMC/CPI/NFP dates are
published in advance, so blocking an entry ON an event date uses no future information.

Public API
----------
fetch_tv_calendar(start, end, ...) -> pd.DataFrame[date, category, title]
"""

from __future__ import annotations

import calendar as _calendar
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from core.fetchers.http import request_with_retry

logger = logging.getLogger(__name__)

_URL = "https://economic-calendar.tradingview.com/events"
_HEADERS = {"Origin": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"}


def _classify(title: str) -> str | None:
    """Map an event title to one of the big-three gate categories, else None.

    FOMC matches the exact decision title only — NOT "FOMC Minutes" (≈3 weeks later)
    or "FOMC Economic Projections". CPI/NFP match the headline release titles.
    """
    t = title or ""
    if "Fed Interest Rate Decision" in t:
        return "FOMC"
    if "Non Farm Payrolls" in t:
        return "NFP"
    if "Inflation Rate" in t:   # "Inflation Rate YoY/MoM", "Core Inflation Rate ..."
        return "CPI"
    return None


def _as_ts(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize()


def _parse(rows: list[dict]) -> pd.DataFrame:
    """Raw API rows → tidy frame of importance≥1 FOMC/CPI/NFP, one row per (date, category)."""
    out = []
    for e in rows:
        cat = _classify(e.get("title", ""))
        if cat is None:
            continue
        # FOMC decisions are importance-0 in TV history (importance-1 only recently),
        # so gate FOMC on the exact title; require importance>=1 for CPI/NFP (filters
        # revisions/prelims and keeps the headline release).
        if cat != "FOMC":
            try:
                if int(e.get("importance", -99)) < 1:
                    continue
            except (TypeError, ValueError):
                continue
        ds = (e.get("date") or "")[:10]
        try:
            ts = pd.Timestamp(ds).normalize()
        except (ValueError, TypeError):
            continue
        out.append({"date": ts, "category": cat, "title": e.get("title", "")})
    df = pd.DataFrame(out, columns=["date", "category", "title"])
    if df.empty:
        return df
    return (df.drop_duplicates(subset=["date", "category"])
              .sort_values("date").reset_index(drop=True))


def _fetch_window(start: date, end: date, countries: str) -> list[dict]:
    url = (f"{_URL}?from={start.isoformat()}T00:00:00.000Z"
           f"&to={end.isoformat()}T00:00:00.000Z&countries={countries}")
    resp = request_with_retry("GET", url, headers=_HEADERS, timeout=30,
                              rate_limit_key="tradingview", rate_limit_interval_s=1.0)
    data = resp.json()
    if data.get("status") != "ok":
        logger.debug("[tv_calendar] status=%s for %s..%s", data.get("status"), start, end)
        return []
    return data.get("result", []) or []


def _load(pq: Path) -> pd.DataFrame:
    if pq.exists():
        try:
            df = pd.read_parquet(pq)
            df["date"] = pd.to_datetime(df["date"]).dt.normalize()
            return df
        except Exception as exc:  # noqa: BLE001 - corrupt cache must not abort
            logger.debug("[tv_calendar] cache read failed: %s", exc)
    return pd.DataFrame(columns=["date", "category", "title"])


def _is_fresh(meta: Path, ttl_hours: float) -> bool:
    try:
        stamp = json.loads(meta.read_text())["fetched_at"]
        age = datetime.now() - datetime.fromisoformat(stamp)
        return age < timedelta(hours=ttl_hours)
    except Exception:  # noqa: BLE001
        return False


def _slice(df: pd.DataFrame, start, end) -> pd.DataFrame:
    if df.empty:
        return df
    lo, hi = _as_ts(start), _as_ts(end)
    return df[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)


def fetch_tv_calendar(
        start, end, *, countries=("US", "CA"),
        cache_dir: str | Path = "data/macro", force: bool = False,
        ttl_hours: float = 24.0,
) -> pd.DataFrame:
    """FOMC/CPI/NFP event dates in ``[start, end]`` from TradingView — cached, fail-open.

    Historical events never change, so the parquet cache grows and is reused; a fetch
    fires only when the cache is missing, stale (> ``ttl_hours``), or doesn't yet cover
    ``end`` (forward extension). Any network failure returns the cached frame.
    Returns a frame ``[date(datetime64), category, title]`` sliced to ``[start, end]``.
    """
    start_ts, end_ts = _as_ts(start), _as_ts(end)
    cdir = Path(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    pq, meta = cdir / "tv_calendar.parquet", cdir / "tv_calendar.meta.json"

    cached = _load(pq)
    covers = (not cached.empty) and (cached["date"].max() >= end_ts) \
        and (cached["date"].min() <= start_ts)
    if not force and covers and _is_fresh(meta, ttl_hours):
        return _slice(cached, start_ts, end_ts)

    cstr = ",".join(countries)
    try:
        rows: list[dict] = []
        # Chunk by MONTH: the API caps its response (~hundreds of all-importance events
        # per request), so a year-wide window silently truncates later events. Monthly
        # windows stay well under the cap. Rate-limited + cached → the cost is one-time.
        s_d, e_d = start_ts.date(), end_ts.date()
        cur = date(s_d.year, s_d.month, 1)
        while cur <= e_d:
            last = _calendar.monthrange(cur.year, cur.month)[1]
            ws = max(s_d, cur)
            we = min(e_d, date(cur.year, cur.month, last))
            rows.extend(_fetch_window(ws, we, cstr))
            cur = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        fetched = _parse(rows)
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("[tv_calendar] fetch failed (%s); serving cache (%d rows)",
                       exc, len(cached))
        return _slice(cached, start_ts, end_ts)

    if fetched.empty and not cached.empty:
        return _slice(cached, start_ts, end_ts)   # keep cache if the pull came back empty

    merged = (pd.concat([cached, fetched], ignore_index=True)
              .drop_duplicates(subset=["date", "category"])
              .sort_values("date").reset_index(drop=True))
    try:
        merged.to_parquet(pq)
        meta.write_text(json.dumps({"fetched_at": datetime.now().isoformat()}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tv_calendar] cache write failed: %s", exc)
    return _slice(merged, start_ts, end_ts)
