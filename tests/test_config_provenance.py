"""Panel config writes are audited, and edge-defining ones say so.

The router's docstring used to claim edge-defining parameters were locked. They
are not — 14 of 21 whitelisted keys change trade composition. The capability is
kept deliberately, so the guard is provenance plus an explicit flag on the
response, not silence.
"""

from __future__ import annotations

import json

import pytest

from api.routers import config as cfgmod


def test_edge_defining_set_covers_every_filters_key():
    """Anything the engine reads out of filters.yaml alters entries."""
    filters_keys = {k for k in cfgmod._EDITABLE if k.startswith("filters.")}
    assert cfgmod._EDGE_DEFINING == filters_keys
    assert len(cfgmod._EDGE_DEFINING) == 14
    assert len(cfgmod._EDITABLE) == 21
    # The knobs that are genuinely operational must NOT be flagged.
    for operational in ("settings.telegram.enabled", "settings.telegram.send_stand_down",
                        "settings.scanner.event_risk_within_days"):
        assert operational not in cfgmod._EDGE_DEFINING
    # …and the ones the audit named must be.
    for edge in ("filters.signals.stop_loss.min_rr", "filters.trend.ma_fast",
                 "filters.execution.max_hold_days",
                 "filters.execution.breakeven_trigger_r", "filters.regime.vix_low"):
        assert edge in cfgmod._EDGE_DEFINING


def test_provenance_records_old_and_new(tmp_path, monkeypatch):
    log = tmp_path / "config_audit.jsonl"
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", log)
    cfgmod._record_provenance([
        {"ts": "2026-07-25T00:00:00+00:00", "key": "filters.trend.ma_fast",
         "old": 50, "new": 40, "file": "filters.yaml", "edge_defining": True},
        {"ts": "2026-07-25T00:00:00+00:00", "key": "settings.telegram.enabled",
         "old": True, "new": False, "file": "settings.yaml", "edge_defining": False},
    ])
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [e["key"] for e in lines] == ["filters.trend.ma_fast",
                                         "settings.telegram.enabled"]
    assert lines[0]["old"] == 50 and lines[0]["new"] == 40
    assert lines[0]["edge_defining"] is True and lines[1]["edge_defining"] is False


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """A real config dir + audit log the write path can operate on."""
    (tmp_path / "settings.yaml").write_text(
        "telegram:\n  enabled: true                 # push toggle\n", encoding="utf-8")
    log = tmp_path / "config_audit.jsonl"
    monkeypatch.setattr(cfgmod, "CONFIG", tmp_path)
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", log)
    return log


def _entries(log):
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def test_successful_write_is_recorded_as_applied(panel):
    body = cfgmod.ConfigWrite(updates={"settings.telegram.enabled": False})
    assert cfgmod.write_config(body)["ok"] is True
    [e] = _entries(panel)
    assert (e["key"], e["old"], e["new"], e["outcome"]) == \
        ("settings.telegram.enabled", True, False, "applied")


def test_failed_commit_still_leaves_an_audit_record(panel, monkeypatch):
    """A fault between the two os.replace calls leaves one file updated and one
    not — the case where 'what actually landed?' is hardest to reconstruct. The
    attempt must be recorded, tagged failed, not dropped."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cfgmod.os, "replace", _boom)
    body = cfgmod.ConfigWrite(updates={"settings.telegram.enabled": False})
    with pytest.raises(Exception):
        cfgmod.write_config(body)
    [e] = _entries(panel)
    assert e["outcome"] == "failed" and "disk full" in e["error"]
    assert e["key"] == "settings.telegram.enabled"


def test_edge_defining_write_flags_a_regression_check(tmp_path, monkeypatch):
    (tmp_path / "filters.yaml").write_text(
        "trend:\n  ma_fast: 50                     # fast MA\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG", tmp_path)
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", tmp_path / "config_audit.jsonl")
    out = cfgmod.write_config(cfgmod.ConfigWrite(updates={"filters.trend.ma_fast": 40}))
    assert out["requires_regression_check"] == ["filters.trend.ma_fast"]


def test_operational_write_flags_nothing(panel):
    out = cfgmod.write_config(cfgmod.ConfigWrite(
        updates={"settings.telegram.enabled": False}))
    assert out["requires_regression_check"] == []


def test_provenance_appends_rather_than_truncates(tmp_path, monkeypatch):
    """The audit trail is the point — a second write must not erase the first."""
    log = tmp_path / "config_audit.jsonl"
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", log)
    cfgmod._record_provenance([{"key": "a", "old": 1, "new": 2}])
    cfgmod._record_provenance([{"key": "b", "old": 3, "new": 4}])
    assert [json.loads(x)["key"] for x in log.read_text(encoding="utf-8").splitlines()] \
        == ["a", "b"]


def test_provenance_failure_never_breaks_the_write(tmp_path, monkeypatch, caplog):
    """A successful config write must not be reported as a failure because the
    audit file could not be appended to."""
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", tmp_path / "nope" / "x.jsonl")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cfgmod.Path, "mkdir", _boom)
    with caplog.at_level("WARNING"):
        cfgmod._record_provenance([{"key": "filters.trend.ma_fast", "old": 1, "new": 2}])
    assert any("provenance was not recorded" in r.message for r in caplog.records)


def test_empty_provenance_writes_nothing(tmp_path, monkeypatch):
    log = tmp_path / "config_audit.jsonl"
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", log)
    cfgmod._record_provenance([])
    assert not log.exists()


@pytest.mark.parametrize("key,expected", [
    ("filters.signals.stop_loss.atr_multiplier", True),
    ("settings.macro.enabled", False),
])
def test_edge_flag_matches_membership(key, expected):
    assert (key in cfgmod._EDGE_DEFINING) is expected
