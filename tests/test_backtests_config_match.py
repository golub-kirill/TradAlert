"""GET /backtests must tag each run with config_match.

The Overview dashboard picks its headline run off this field. Without it every
row looks eligible and the panel silently reverts to headlining the newest run —
which is how an A/B leg (run 34, a VIX-slope-gated arm) came to advertise 44.32R
as the strategy's net total R beside the baseline's 90.83R. `tsc` cannot catch
the regression either: the field is optional on the client type.
"""

from __future__ import annotations

import json

import pytest

from api.routers import backtests as bt


@pytest.fixture
def rows(monkeypatch):
    """Two runs: a baseline on the shipped config and an A/B leg with an extra
    gate on. Only the DB read and the shipped-config read are stubbed."""
    shipped = {"regime.vix_slope_block": False, "trend.ma_fast": 50}

    def _cfg(vix_block: bool) -> str:
        return json.dumps({"regime": {"vix_slope_block": vix_block},
                           "trend": {"ma_fast": 50},
                           "_meta": {"use_scoring": False}})

    fake = [
        {"id": 34, "started_at": None, "start_date": None, "end_date": None,
         "trades_count": 1651, "total_r": 44.32, "expectancy_r": 0.027,
         "profit_factor": 1.11, "win_rate": 0.42, "max_drawdown_r": 27.5,
         "notes": None, "config_json": _cfg(True)},
        {"id": 33, "started_at": None, "start_date": None, "end_date": None,
         "trades_count": 1735, "total_r": 90.83, "expectancy_r": 0.052,
         "profit_factor": 1.21, "win_rate": 0.41, "max_drawdown_r": 42.9,
         "notes": None, "config_json": _cfg(False)},
    ]
    monkeypatch.setattr(bt, "query", lambda *a, **k: [dict(r) for r in fake])
    monkeypatch.setattr("backtest.db._shipped_filters", lambda: shipped)
    monkeypatch.setattr(bt, "load_yaml", lambda name: {})
    return fake


def test_experiment_arm_is_tagged_not_matching(rows):
    out = {r["id"]: r for r in bt.backtests(limit=2)}
    assert out[34]["config_match"] is False
    assert any("vix_slope_block" in m for m in out[34]["config_mismatch"])


def test_baseline_is_tagged_matching(rows):
    out = {r["id"]: r for r in bt.backtests(limit=2)}
    assert out[33]["config_match"] is True
    assert out[33]["config_mismatch"] == []


def test_config_json_is_not_leaked_to_the_client(rows):
    """The raw snapshot is popped — it is large and the panel never reads it."""
    assert all("config_json" not in r for r in bt.backtests(limit=2))


def test_unknown_snapshot_stays_eligible(monkeypatch):
    """No config_json → config_match None, which the dashboard treats as usable.
    Rejecting it would leave no baseline at all on a legacy journal."""
    monkeypatch.setattr(bt, "query", lambda *a, **k: [
        {"id": 9, "started_at": None, "start_date": None, "end_date": None,
         "trades_count": 10, "total_r": 1.0, "expectancy_r": 0.1,
         "profit_factor": 1.0, "win_rate": 0.5, "max_drawdown_r": 1.0,
         "notes": None, "config_json": None}])
    monkeypatch.setattr(bt, "load_yaml", lambda name: {})
    [row] = bt.backtests(limit=1)
    assert row["config_match"] is None and row["config_mismatch"] == []


def test_shipped_config_is_read_once_not_per_row(monkeypatch, rows):
    """It parses filters.yaml; doing it per row put ~20 parses on the landing
    view's hot path."""
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return {"regime.vix_slope_block": False, "trend.ma_fast": 50}

    monkeypatch.setattr("backtest.db._shipped_filters", _counting)
    bt.backtests(limit=2)
    assert calls["n"] == 1
