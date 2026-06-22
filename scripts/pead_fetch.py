#!/usr/bin/env python3
"""
Deeper earnings re-fetch for the PEAD gate (PEAD-1, pre-reg `docs/backtest_out/pead_gate_prereg.md`).

The production fetcher (`core.fetchers.earnings_history`) keeps only the earnings
*date* — it discards the announcement TIME and the EPS estimate/reported/surprise
columns. The PEAD gate needs the announcement time (to align the reaction window
BMO vs AMC without look-ahead) and the surprise (for the reported-not-gated SUE
variant). So this is a one-off, separate, resumable re-fetch into its OWN cache
(`data/earnings_history_pead/`) — the live/backtest date-only caches are untouched.

Per-ticker output `data/earnings_history_pead/{TICKER}.parquet` (one row per event):
    ann_date (str YYYY-MM-DD, exchange-local), local_hour (int; -1 = unknown/placeholder),
    eps_estimate, reported_eps, surprise_pct
ETFs / no-earnings names write a 0-row parquet (so the resume-skip works).

    .venv/Scripts/python.exe scripts/pead_fetch.py [--force] [--limit 120]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
import yaml          # noqa: E402

from core.fetchers.symbology import to_yf_symbol      # noqa: E402
from persistence.json_cache import silence_yfinance   # noqa: E402

OUT = _ROOT / "data" / "earnings_history_pead"
WATCHLIST = _ROOT / "config" / "watchlist.yaml"
_COLS = ["ann_date", "local_hour", "eps_estimate", "reported_eps", "surprise_pct"]


def load_tier_a() -> list[str]:
    """tier_a symbols (skip the pure context series); ETFs stay in — they just fetch empty."""
    data = yaml.safe_load(WATCHLIST.read_text(encoding="utf-8")) or {}
    names = data.get("tier_a", []) or []
    skip = {"^VIX"}
    out, seen = [], set()
    for t in names:
        s = str(t).strip()
        if s and s not in skip and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def fetch_one(ticker: str, limit: int) -> pd.DataFrame:
    """Query yfinance earnings_dates with surprise columns + the announcement time."""
    import yfinance as yf
    yt = yf.Ticker(to_yf_symbol(ticker))
    limit = min(int(limit), 100)   # Yahoo caps the limit at 100; >100 raises
    with silence_yfinance():
        try:
            df = yt.get_earnings_dates(limit=limit)
        except Exception:
            df = getattr(yt, "earnings_dates", None)
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=_COLS)

    cols = {c.lower(): c for c in df.columns}
    est_c = next((cols[k] for k in cols if "estimate" in k), None)
    rep_c = next((cols[k] for k in cols if "reported" in k), None)
    sur_c = next((cols[k] for k in cols if "surprise" in k), None)

    rows = []
    for ts, r in df.iterrows():
        try:
            ts = pd.Timestamp(ts)
        except Exception:
            continue
        # .hour is in the timestamp's own tz (exchange-local). A 00:00 stamp is a
        # yfinance placeholder for "time unknown" → mark -1 (the gate defaults it to AMC).
        local_hour = -1 if (ts.hour == 0 and ts.minute == 0) else int(ts.hour)
        rows.append(dict(
            ann_date=ts.date().isoformat(),
            local_hour=local_hour,
            eps_estimate=_f(r.get(est_c)) if est_c else float("nan"),
            reported_eps=_f(r.get(rep_c)) if rep_c else float("nan"),
            surprise_pct=_f(r.get(sur_c)) if sur_c else float("nan"),
        ))
    return pd.DataFrame(rows, columns=_COLS)


def main() -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Deeper earnings re-fetch for the PEAD gate")
    ap.add_argument("--force", action="store_true", help="re-fetch even if a cache file exists")
    ap.add_argument("--limit", type=int, default=100, help="yfinance get_earnings_dates limit (Yahoo caps at 100)")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between calls")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    tickers = load_tier_a()
    print(f"PEAD re-fetch: {len(tickers)} tier_a names → {OUT}", flush=True)

    n_done = n_skip = n_empty = n_err = 0
    for i, tk in enumerate(tickers, 1):
        path = OUT / f"{tk.upper()}.parquet"
        if path.exists() and not args.force:
            n_skip += 1
            continue
        try:
            df = fetch_one(tk, args.limit)
            df.to_parquet(path, index=False)
            n_done += 1
            if len(df) == 0:
                n_empty += 1
            print(f"  [{i}/{len(tickers)}] {tk}: {len(df)} events", flush=True)
        except Exception as e:  # noqa: BLE001 — fail-open per ticker
            n_err += 1
            print(f"  [{i}/{len(tickers)}] {tk}: ERROR {e!r}", flush=True)
        time.sleep(args.sleep)

    print(f"done — fetched {n_done} (empty {n_empty}), skipped {n_skip}, errors {n_err}", flush=True)


if __name__ == "__main__":
    main()
