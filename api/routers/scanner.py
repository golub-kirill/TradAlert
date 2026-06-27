"""Scan journal: recent runs + the latest run's fired signals and stand-down."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import load_company_names, query
from api.jobs import launch, python_exe

router = APIRouter(tags=["scanner"])


@router.get("/scanner/runs")
def runs(limit: int = 25):
    return query(
        "SELECT id, created_at, market_regime, tickers_scanned, scan_passed, "
        "signals_fired FROM scan_runs ORDER BY id DESC LIMIT %s",
        (int(limit),),
    )


@router.get("/scanner/latest")
def latest():
    from persistence.db import latest_scan_run
    run = latest_scan_run()
    if not run:
        return {"run": None, "fired": [], "stand_down": None}
    rid = run["run_id"]
    fired = query(
        "SELECT ticker, signal_kind, signal_type, `close`, stop_price, target_price, "
        "tier, review_reason, reason FROM scan_results WHERE run_id=%s AND "
        "signal_kind IN ('entry_long','entry_short','exit_long','exit_short') "
        "ORDER BY signal_kind, ticker",
        (rid,),
    )
    names = load_company_names()
    for r in fired:
        r["name"] = names.get(r["ticker"])
    stand_down = None
    try:
        from persistence.db import stand_down_summary
        stand_down = stand_down_summary(rid)
    except Exception:
        pass
    return {"run": run, "fired": fired, "stand_down": stand_down}


class ScanReq(BaseModel):
    morning: bool = False
    force: bool = False


@router.post("/scan")
def run_scan(req: ScanReq):
    """Trigger a live scan by shelling ``python main.py`` as a background job.

    Returns a ``job_id`` streamable via ``/api/backtests/jobs/{id}/stream``.
    Results land in the journal; re-fetch ``/scanner/latest`` once it finishes.
    """
    cmd = [python_exe(), "main.py"]
    if req.force:
        cmd.append("--force")
    if req.morning:
        cmd.append("--morning")
    jid = launch(cmd)
    return {"job_id": jid, "cmd": " ".join(cmd)}
