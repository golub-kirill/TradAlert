"""Minimal in-process background-job runner for action endpoints.

A backtest is minutes long, so action routes enqueue the real script as a
subprocess and return a job id; the UI polls status. In-memory only (a restart
forgets jobs) — fine for a single-operator tool; swap for RQ/arq if it grows.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
import sys
import threading
import uuid

from api import ROOT

_JOBS: dict[str, dict] = {}
_MAX_JOBS = 50  # bound the in-memory registry; evict oldest finished jobs past this
_MAX_RUNNING = 3  # refuse new launches past this many live subprocesses (each is
#                   a minutes-long backtest/scan — stacking them starves the box)


def _evict() -> None:
    """Drop the oldest finished jobs so a long-lived server can't leak memory."""
    for jid in list(_JOBS):
        if len(_JOBS) <= _MAX_JOBS:
            break
        if _JOBS[jid]["status"] != "running":
            del _JOBS[jid]

# Strip ANSI escape sequences (the backtest report prints SGR colours) so the
# web log shows clean text instead of literal "[32m…" codes.
_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _clean(line: str) -> str:
    return _ANSI.sub("", line).rstrip()


def launch(cmd: list[str]) -> str:
    """Start ``cmd`` (a python module invocation) under the repo root, tracked by id.

    Raises HTTPException 429 when ``_MAX_RUNNING`` jobs are already live — the
    routers pass it straight through, so a click-happy UI can't stack heavy
    subprocess runs.
    """
    _evict()
    running = sum(1 for j in _JOBS.values() if j["status"] == "running")
    if running >= _MAX_RUNNING:
        from fastapi import HTTPException
        raise HTTPException(
            429, f"{running} jobs already running (max {_MAX_RUNNING}) — "
                 "wait for one to finish")
    jid = uuid.uuid4().hex[:8]
    _JOBS[jid] = {
        "status": "running",
        "cmd": " ".join(cmd),
        "lines": collections.deque(maxlen=4000),
        "total": 0,  # monotonic line count (for incremental SSE tailing)
        "returncode": None,
    }
    threading.Thread(target=_run, args=(jid, cmd), daemon=True).start()
    return jid


def _run(jid: str, cmd: list[str]) -> None:
    job = _JOBS[jid]
    try:
        # The child scripts force UTF-8 stdout; decode the pipe as UTF-8 to match
        # (Windows would otherwise decode cp1252 and mojibake box/arrow glyphs).
        # PYTHONIOENCODING is std-stream only, so it can't affect engine results.
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        for line in proc.stdout:
            job["lines"].append(_clean(line))
            job["total"] += 1
        proc.wait()
        job["returncode"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        job["lines"].append(f"launch failed: {exc}")
        job["status"] = "error"


def get(jid: str) -> dict | None:
    """The raw job record (for live streaming), or None if unknown."""
    return _JOBS.get(jid)


def status(jid: str) -> dict:
    job = _JOBS.get(jid)
    if not job:
        return {"status": "unknown"}
    return {
        "status": job["status"],
        "returncode": job["returncode"],
        "cmd": job["cmd"],
        "tail": list(job["lines"])[-50:],
    }


def python_exe() -> str:
    """The interpreter running this server — the venv python when launched from it."""
    return sys.executable
