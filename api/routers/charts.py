"""OHLCV + indicators for a ticker, straight from the parquet cache."""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["charts"])

_COLS = ["open", "high", "low", "close", "ma_fast", "ma_slow", "rsi",
         "macd", "macd_signal", "macd_hist", "bb_upper", "bb_lower"]


@router.get("/charts/{ticker}")
def chart(ticker: str, days: int = 160):
    try:
        from persistence.cache import load as cache_load
        from core.indicators.indicators import attach_indicators
        from exceptions import ValidationError
    except Exception as exc:
        raise HTTPException(500, f"indicator import failed: {exc}")
    try:
        df = cache_load(ticker)
    except ValidationError:
        raise HTTPException(400, f"invalid ticker {ticker!r}")
    except Exception:
        raise HTTPException(404, f"no cached data for {ticker}")
    try:
        df = attach_indicators(df).tail(int(days))
    except Exception as exc:
        raise HTTPException(500, f"indicator computation failed: {exc}")
    bars = []
    for idx, row in df.iterrows():
        rec = {"date": str(getattr(idx, "date", lambda: idx)())}
        for c in _COLS:
            v = row.get(c)
            rec[c] = None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 4)
        bars.append(rec)
    return {"ticker": ticker.upper(), "bars": bars}
