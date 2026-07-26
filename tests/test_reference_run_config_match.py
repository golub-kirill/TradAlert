"""The expectancy reference must be a BASELINE, not an experiment arm.

On 2026-07-19 run 34 — a paired-A/B leg with ``regime.vix_slope_block`` ON,
+44.32R against the +90.83R baseline journaled sixteen minutes earlier — was
full-window and scoring-OFF, so the old ladder picked it as the drift reference.
Every live-vs-backtest reading was then taken against the gated arm.
"""

from __future__ import annotations

import json

from backtest.db import config_mismatch, reference_run

_SHIPPED = {"regime.vix_slope_block": False, "trend.ma_fast": 50,
            "execution.exit_slippage_pct": 0.002}


def _cfg(**over) -> str:
    base = {"regime": {"vix_slope_block": False},
            "trend": {"ma_fast": 50},
            "execution": {"exit_slippage_pct": 0.002},
            "_meta": {"use_scoring": False}}
    for dotted, val in over.items():
        node = base
        *parents, leaf = dotted.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = val
    return json.dumps(base)


class _Cursor:
    """Minimal dictionary-cursor stand-in over a fixed row list."""

    def __init__(self, rows):
        self._rows, self._last = rows, []

    def execute(self, sql, params=None):
        if params is not None:
            self._last = [dict(r) for r in self._rows if r["id"] == params[0]]
        else:
            self._last = [dict(r) for r in self._rows]

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def _row(run_id, config_json, **over):
    base = dict(id=run_id, start_date=None, end_date=None, trades_count=1700,
                expectancy_r=0.05, win_rate=0.42, notes=None,
                config_json=config_json)
    base.update(over)
    return base


# ── config_mismatch ───────────────────────────────────────────────────────────

def test_matching_config_reports_no_mismatch():
    assert config_mismatch(_cfg(), _SHIPPED) == []


def test_experiment_arm_is_reported_with_the_offending_key():
    diff = config_mismatch(_cfg(**{"regime.vix_slope_block": True}), _SHIPPED)
    assert len(diff) == 1 and "regime.vix_slope_block" in diff[0]
    assert "run=True" in diff[0] and "shipped=False" in diff[0]


def test_meta_and_scan_gates_are_excluded():
    """_meta is run bookkeeping; scan gates can't change a backtest's trades
    because the backtester never calls scan()."""
    cfg = json.dumps({"regime": {"vix_slope_block": False}, "trend": {"ma_fast": 50},
                      "execution": {"exit_slippage_pct": 0.002},
                      "_meta": {"use_scoring": False, "start_date": "2020-01-01"},
                      "price": {"min_price": 999.0},
                      "liquidity": {"min_dollar_volume_20d": 1}})
    assert config_mismatch(cfg, _SHIPPED) == []


def test_sector_gate_is_excluded_while_the_map_is_empty():
    """Flipping an inert switch must not invalidate every historical reference."""
    cfg = _cfg(**{"signals.sector_gate.enabled": True})
    assert config_mismatch(cfg, {**_SHIPPED, "signals.sector_gate.enabled": False}) == []


def test_unknown_config_is_not_a_mismatch():
    """No snapshot / unparseable → None (eligible), never a false rejection."""
    assert config_mismatch(None, _SHIPPED) is None
    assert config_mismatch("not json", _SHIPPED) is None
    assert config_mismatch(_cfg(), None) is None or isinstance(
        config_mismatch(_cfg(), None), list)


# ── reference_run selection ───────────────────────────────────────────────────

def test_experiment_arm_loses_to_the_older_baseline(monkeypatch):
    """The exact 2026-07-19 shape: the arm journals LAST and must still lose."""
    monkeypatch.setattr("backtest.db._shipped_filters", lambda: _SHIPPED)
    cur = _Cursor([
        _row(34, _cfg(**{"regime.vix_slope_block": True}), trades_count=1651,
             expectancy_r=0.0268),
        _row(33, _cfg(), trades_count=1735, expectancy_r=0.0523),
    ])
    ref = reference_run(cur)
    assert ref["id"] == 33
    assert ref["config_match"] is True and ref["config_mismatch"] == []


def test_falls_back_loudly_when_nothing_matches(monkeypatch):
    """No baseline on record → still return a reference, but flag it, so the
    caller can say the drift is not like-for-like instead of going quiet."""
    monkeypatch.setattr("backtest.db._shipped_filters", lambda: _SHIPPED)
    cur = _Cursor([_row(34, _cfg(**{"regime.vix_slope_block": True}))])
    ref = reference_run(cur)
    assert ref["id"] == 34
    assert ref["config_match"] is False
    assert any("vix_slope_block" in line for line in ref["config_mismatch"])


def test_explicit_run_id_is_honoured_but_still_tagged(monkeypatch):
    """--bt-run-id overrides the choice; it must not suppress the warning."""
    monkeypatch.setattr("backtest.db._shipped_filters", lambda: _SHIPPED)
    cur = _Cursor([_row(34, _cfg(**{"regime.vix_slope_block": True})), _row(33, _cfg())])
    ref = reference_run(cur, run_id=34)
    assert ref["id"] == 34 and ref["config_match"] is False


def test_windowed_run_still_loses_to_a_full_window_baseline(monkeypatch):
    """The pre-existing full-window guard must survive the new filter."""
    monkeypatch.setattr("backtest.db._shipped_filters", lambda: _SHIPPED)
    windowed = json.loads(_cfg())
    windowed["_meta"]["start_date"] = "2025-01-01"
    cur = _Cursor([_row(35, json.dumps(windowed), trades_count=59), _row(33, _cfg())])
    assert reference_run(cur)["id"] == 33


def test_no_runs_returns_none():
    assert reference_run(_Cursor([])) is None
