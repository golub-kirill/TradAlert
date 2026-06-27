"""Held positions: read with live unrealized R, plus journal-only edits.

All mutations go through ``core.execution.adapter.get_adapter()`` — the same
journal-only seam the CLI and Telegram bot use. It records to the ``positions``
table and NEVER places a real order (the no-auto-execute invariant lives there).
"""

from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.deps import query

router = APIRouter(tags=["positions"])


def _adapter():
    from core.execution.adapter import get_adapter
    return get_adapter()


def _as_date(s: str | None):
    return _date.fromisoformat(s) if s else _date.today()


def _last_close(ticker: str):
    try:
        from persistence.cache import load as cache_load
        return float(cache_load(ticker)["close"].dropna().iloc[-1])
    except Exception:
        return None


@router.get("/positions")
def positions():
    rows = query(
        "SELECT id, ticker, side, entry_price, entry_date, stop_price, initial_stop "
        "FROM positions WHERE exit_date IS NULL ORDER BY entry_date"
    )
    out = []
    for r in rows:
        entry = float(r["entry_price"])
        stop = r["initial_stop"] if r["initial_stop"] is not None else r["stop_price"]
        stop = float(stop) if stop is not None else None
        now = _last_close(r["ticker"])
        risk = unrealized_r = None
        if stop is not None:
            risk = (entry - stop) if r["side"] == "long" else (stop - entry)
            if now is not None and risk:
                gain = (now - entry) if r["side"] == "long" else (entry - now)
                unrealized_r = round(gain / risk, 3)
        out.append({
            "id": r["id"], "ticker": r["ticker"], "side": r["side"],
            "entry_price": entry, "entry_date": str(r["entry_date"]),
            "stop_price": float(r["stop_price"]) if r["stop_price"] is not None else None,
            "current": now, "unrealized_r": unrealized_r,
        })
    return out


class OpenBody(BaseModel):
    ticker: str
    entry_price: float
    side: str = "long"
    stop_price: float | None = None
    entry_date: str | None = None
    notes: str = ""


class StopBody(BaseModel):
    stop_price: float | None = None


class CloseBody(BaseModel):
    exit_price: float
    exit_date: str | None = None


class ScaleBody(BaseModel):
    exit_price: float
    fraction: float
    exit_date: str | None = None


class EditBody(BaseModel):
    entry_price: float | None = None
    stop_price: float | None = None
    initial_stop: float | None = None
    exit_price: float | None = None
    notes: str | None = None


@router.post("/positions")
def open_position(body: OpenBody):
    # Canonical symbol check (allows ^index / dual-class, normalizes case). This is a
    # journal DB write, not an argv splice, so the CLI-flag-hardened TICKER_RE is the
    # wrong tool here — it would reject legitimate symbols like ^VIX.
    from core.validators.yf_tickerValidator import validate_ticker
    try:
        ticker = validate_ticker(body.ticker)
    except Exception:
        raise HTTPException(400, f"invalid ticker {body.ticker!r}")
    if body.side not in ("long", "short"):
        raise HTTPException(400, "side must be 'long' or 'short'")
    try:
        nid = _adapter().open(
            ticker, body.entry_price, _as_date(body.entry_date),
            side=body.side, stop_price=body.stop_price, notes=body.notes,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc))
    if not nid:
        raise HTTPException(400, "open failed (DB unavailable)")
    return {"ok": True, "id": nid}


@router.patch("/positions/{pid}/stop")
def update_stop(pid: int, body: StopBody):
    try:
        ok = _adapter().update_stop(pid, body.stop_price)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, f"position #{pid} not found or unchanged")
    return {"ok": True, "id": pid, "stop_price": body.stop_price}


@router.post("/positions/{pid}/close")
def close_position(pid: int, body: CloseBody):
    try:
        ok = _adapter().close(pid, body.exit_price, _as_date(body.exit_date))
    except Exception as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, f"position #{pid} not open or not found")
    return {"ok": True, "id": pid, "exit_price": body.exit_price}


@router.post("/positions/{pid}/scale-out")
def scale_out(pid: int, body: ScaleBody):
    try:
        res = _adapter().scale_out(pid, body.exit_price, _as_date(body.exit_date), body.fraction)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    if not res:
        raise HTTPException(400, "scale-out failed (check id and fraction)")
    return {"ok": True, "id": pid, "partial_id": res}


@router.patch("/positions/{pid}")
def edit_position(pid: int, body: EditBody):
    try:
        ok = _adapter().edit_position(
            pid, entry_price=body.entry_price, stop_price=body.stop_price,
            initial_stop=body.initial_stop, exit_price=body.exit_price, notes=body.notes,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, f"position #{pid} not found or no change")
    return {"ok": True, "id": pid}
