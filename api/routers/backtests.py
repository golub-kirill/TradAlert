"""Backtest history (read) + run launcher (enqueues the real run_backtest)."""

from __future__ import annotations

import asyncio
import re
from datetime import date as _date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.deps import query
from api.jobs import get as job_get, launch, python_exe, status as job_status

router = APIRouter(tags=["backtests"])

# A symbol must start alphanumeric so it can never be parsed as a CLI flag when
# spliced into the run_backtest argv (e.g. a "--out" "ticker" can't redirect output).
_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,15}$")


def _sse(event: str, data: str) -> str:
    """One Server-Sent Event frame (data must be newline-free per line)."""
    return f"event: {event}\ndata: {data}\n\n"


def _check_date(s: str | None, field: str) -> None:
    if s:
        try:
            _date.fromisoformat(s)
        except ValueError:
            raise HTTPException(400, f"{field} must be an ISO date (YYYY-MM-DD)")

_MODES = {
    "baseline": [],
    "sweep": ["--sweep"],
    "walk-forward": ["--walk-forward"],
    "robustness": ["--robustness"],
}


@router.get("/backtests")
def backtests(limit: int = 20):
    return query(
        "SELECT id, started_at, start_date, end_date, trades_count, total_r, "
        "expectancy_r, profit_factor, win_rate, max_drawdown_r, notes "
        "FROM backtest_runs ORDER BY id DESC LIMIT %s",
        (int(limit),),
    )


@router.get("/backtests/{run_id}/trades")
def trades(run_id: int, limit: int = 500):
    return query(
        "SELECT ticker, direction, signal_type, entry_date, exit_date, exit_reason, "
        "r_multiple, effective_r, market_regime FROM backtest_trades "
        "WHERE run_id=%s ORDER BY exit_date DESC LIMIT %s",
        (int(run_id), int(limit)),
    )


@router.get("/backtests/{run_id}/equity")
def equity(run_id: int):
    """Cumulative R equity curve for a run, aggregated by exit date.

    Uses size/borrow-adjusted ``effective_r`` (falls back to ``r_multiple``) so the
    final point matches the run's net total R. One point per closing date.
    """
    rows = query(
        "SELECT exit_date, COALESCE(effective_r, r_multiple) AS r FROM backtest_trades "
        "WHERE run_id=%s AND exit_date IS NOT NULL ORDER BY exit_date, id",
        (int(run_id),),
    )
    by_date: dict[str, float] = {}
    order: list[str] = []
    for row in rows:
        d = str(row["exit_date"])
        if d not in by_date:
            by_date[d] = 0.0
            order.append(d)
        by_date[d] += float(row["r"] or 0.0)
    cum = 0.0
    points = []
    for d in order:
        cum += by_date[d]
        points.append({"date": d, "equity_r": round(cum, 4)})
    return {"run_id": run_id, "points": points}


@router.get("/backtests/{run_id}/monthly")
def monthly(run_id: int):
    """Per-month equity candles (open/high/low/close of cumulative R) + W/L counts.

    Drives the Overview performance chart: green months (close>=open) vs red, plus
    overall win-rate and the share of up months.
    """
    rows = query(
        "SELECT exit_date, COALESCE(effective_r, r_multiple) AS r FROM backtest_trades "
        "WHERE run_id=%s AND exit_date IS NOT NULL ORDER BY exit_date, id",
        (int(run_id),),
    )
    cum = 0.0
    months: dict[str, dict] = {}
    order: list[str] = []
    wins = losses = 0
    for row in rows:
        ym = str(row["exit_date"])[:7]  # YYYY-MM
        rv = float(row["r"] or 0.0)
        if ym not in months:
            months[ym] = {"month": ym, "open": cum, "high": cum, "low": cum, "close": cum,
                          "r": 0.0, "wins": 0, "losses": 0}
            order.append(ym)
        m = months[ym]
        cum += rv
        m["close"] = cum
        m["high"] = max(m["high"], cum)
        m["low"] = min(m["low"], cum)
        m["r"] += rv
        if rv > 0:
            m["wins"] += 1
            wins += 1
        else:
            m["losses"] += 1
            losses += 1
    out = []
    for ym in order:
        m = months[ym]
        out.append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()})
    up_months = sum(1 for m in out if m["close"] >= m["open"])
    total = wins + losses
    return {
        "run_id": run_id,
        "months": out,
        "win_rate": round(wins / total, 4) if total else None,
        "up_month_pct": round(up_months / len(out), 4) if out else None,
        "wins": wins,
        "losses": losses,
    }


class BacktestReq(BaseModel):
    start: str | None = None
    end: str | None = None
    mode: str = "baseline"
    max_open_risk: float | None = None
    breakeven_trigger_r: float | None = None
    max_hold_days: int | None = None
    allow_shorts: bool = False
    tickers: list[str] | None = None


@router.post("/backtests/run")
def run(req: BacktestReq):
    _check_date(req.start, "start")
    _check_date(req.end, "end")
    if req.tickers:
        for t in req.tickers:
            if not _TICKER_RE.match(t):
                raise HTTPException(400, f"invalid ticker {t!r}")
    cmd = [python_exe(), "-m", "backtest.run_backtest", *_MODES.get(req.mode, [])]
    if req.start:
        cmd += ["--start", req.start]
    if req.end:
        cmd += ["--end", req.end]
    if req.max_open_risk is not None:
        cmd += ["--max-open-risk", str(req.max_open_risk)]
    if req.breakeven_trigger_r is not None:
        cmd += ["--breakeven-trigger-r", str(req.breakeven_trigger_r)]
    if req.max_hold_days is not None:
        cmd += ["--max-hold-days", str(req.max_hold_days)]
    if req.allow_shorts:
        cmd += ["--allow-shorts"]
    if req.tickers:
        cmd += ["--tickers", *req.tickers]
    jid = launch(cmd)
    return {"job_id": jid, "cmd": " ".join(cmd)}


@router.get("/backtests/jobs/{jid}")
def job(jid: str):
    return job_status(jid)


@router.get("/backtests/jobs/{jid}/stream")
async def job_stream(jid: str, request: Request):
    """Live-tail a job's output as Server-Sent Events (``line`` + ``status``).

    Generic over any job in the registry (backtests and live scans both use it).
    Emits each new output line, then a terminal ``status`` event when the job
    finishes, and closes. Async + disconnect-aware: if the client goes away the
    generator stops promptly instead of polling until the job ends.
    """

    def _emit_new(rec, last):
        total = rec["total"]
        if total <= last:
            return [], last
        lines = list(rec["lines"])
        fresh = lines[-(total - last):] if (total - last) <= len(lines) else lines
        return fresh, total

    async def gen():
        last = 0
        while True:
            rec = job_get(jid)
            if rec is None:
                yield _sse("status", "unknown")
                return
            fresh, last = _emit_new(rec, last)
            for ln in fresh:
                yield _sse("line", ln)
            status = rec["status"]
            if status != "running":
                # final drain: flush any lines that landed between the snapshot and exit
                fresh, last = _emit_new(rec, last)
                for ln in fresh:
                    yield _sse("line", ln)
                yield _sse("status", status)
                return
            yield _sse("status", status)
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
