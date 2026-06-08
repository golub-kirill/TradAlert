"""Reconciliation provenance + effective_r aggregation + replay parity (P1 #7/#8/#9).

Covers:
  * backtest.db.reference_run    — prefer the latest scoring-OFF run (#9a)
  * backtest.db.trade_r_column   — aggregate effective_r when present, else r_multiple (#7)
  * backtest.db._run_use_scoring — parse _meta.use_scoring from a config snapshot
  * reconcile_live._replay       — max-hold mode parity with the backtester (#8)
No DB: the cursor is a tiny in-memory fake.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.db import reference_run, trade_r_column, _run_use_scoring  # noqa: E402


class _FakeCursor:
    """Minimal dict-cursor: answers the column probe and the run queries."""

    def __init__(self, *, has_col=True, runs=None):
        self.has_col = has_col
        self.runs = runs or []           # newest-first, like ORDER BY id DESC
        self._rows = []

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        if "information_schema.columns" in s:
            self._rows = [{"n": 1 if self.has_col else 0}]
        elif "where id = %s" in s:
            self._rows = [dict(r) for r in self.runs if r["id"] == params[0]]
        elif "order by id desc" in s:
            self._rows = [dict(r) for r in self.runs]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _run(rid, use_scoring):
    meta = {} if use_scoring is None else {"use_scoring": use_scoring}
    return {"id": rid, "start_date": None, "end_date": None, "trades_count": 0,
            "expectancy_r": 0.0, "win_rate": 0.0, "notes": f"run{rid}",
            "config_json": json.dumps({"_meta": meta})}


# ── _run_use_scoring ──────────────────────────────────────────────────────────

def test_run_use_scoring_parses_meta():
    assert _run_use_scoring(json.dumps({"_meta": {"use_scoring": False}})) is False
    assert _run_use_scoring(json.dumps({"_meta": {"use_scoring": True}})) is True
    assert _run_use_scoring(json.dumps({"_meta": {}})) is None
    assert _run_use_scoring(None) is None
    assert _run_use_scoring("not json") is None


# ── trade_r_column (#7) ───────────────────────────────────────────────────────

def test_trade_r_column_prefers_effective_r_when_present():
    assert trade_r_column(_FakeCursor(has_col=True)) == "effective_r"
    assert trade_r_column(_FakeCursor(has_col=False)) == "r_multiple"


# ── reference_run provenance (#9a) ────────────────────────────────────────────

def test_reference_run_prefers_latest_scoring_off():
    # Newest (id 3) is scoring-ON; the reconciler must skip it for the scoring-OFF run.
    runs = [_run(3, True), _run(2, False), _run(1, None)]
    chosen = reference_run(_FakeCursor(runs=runs))
    assert chosen["id"] == 2
    assert "config_json" not in chosen          # stripped before return


def test_reference_run_falls_back_to_newest_when_none_tagged_off():
    runs = [_run(3, True), _run(2, None), _run(1, True)]
    assert reference_run(_FakeCursor(runs=runs))["id"] == 3


def test_reference_run_explicit_id_wins():
    runs = [_run(3, False), _run(2, False), _run(1, False)]
    assert reference_run(_FakeCursor(runs=runs), run_id=1)["id"] == 1


# ── reconcile_live._replay max-hold parity (#8) ───────────────────────────────

def test_replay_max_hold_mode_parity():
    from scripts.reconcile_live import _replay
    from backtest.backtester import (apply_stop_fill, apply_target_fill,
                                      apply_stop_fill_short, apply_target_fill_short)

    idx = pd.date_range("2024-01-01", periods=8, freq="B")
    close = [100, 101, 102, 103, 104, 105, 106, 107]   # rising → long is in profit
    df = pd.DataFrame(
        {"open": close, "high": [c + 0.5 for c in close],
         "low": [c - 0.5 for c in close], "close": close},
        index=idx,
    )
    # entry T+1 at idx 1; stop/target placed so neither is ever touched.
    args = (df, 1, 100.0, 90.0, 200.0, False, 3)
    fills = (apply_stop_fill, apply_target_fill, apply_stop_fill_short, apply_target_fill_short)

    # hard: force-close at the cap regardless of P&L.
    _px, reason_hard = _replay(*args, "hard", *fills)
    assert reason_hard == "time_stop"

    # if_not_profit: in profit at the cap → do NOT exit (parity with the backtester).
    px_inp, reason_inp = _replay(*args, "if_not_profit", *fills)
    assert px_inp is None and reason_inp == "pending"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
