"""Warm a ticker -> company-name cache (data/company_names.json) for the UI.

One-time / occasional: pulls longName via yfinance for the watchlist tier_a
(skips names already cached). Fail-open per ticker. The control API reads the
JSON to label signals/positions with the full company name.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402
import yfinance as yf  # noqa: E402
from core.fetchers.symbology import to_yf_symbol  # noqa: E402

OUT = ROOT / "data" / "company_names.json"


def main() -> None:
    wl = yaml.safe_load(open(ROOT / "config" / "watchlist.yaml", encoding="utf-8"))
    tickers = [t for t in (wl.get("tier_a") or []) if t != "^VIX"]

    names: dict[str, str] = {}
    if OUT.exists():
        try:
            names = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            names = {}

    todo = [t for t in tickers if t not in names]
    print(f"warming {len(todo)} / {len(tickers)} names …", flush=True)
    for i, t in enumerate(todo, 1):
        try:
            info = yf.Ticker(to_yf_symbol(t)).info
            nm = info.get("longName") or info.get("shortName")
            if nm:
                names[t] = str(nm).strip()
        except Exception:
            pass
        if i % 20 == 0:
            json.dump(names, open(OUT, "w", encoding="utf-8"), indent=0, ensure_ascii=False)
            print(f"  {i}/{len(todo)}  {t} -> {names.get(t)}", flush=True)
            time.sleep(0.2)

    json.dump(names, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"done — {len(names)} names cached at {OUT}", flush=True)


if __name__ == "__main__":
    main()
