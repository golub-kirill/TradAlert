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
