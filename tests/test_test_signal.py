"""Read-only signal-probe contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.types import ScanResult, SignalResult
from scripts.live import test_signal as probe


class _BlockedEngine:
    def scan(self, *args, **kwargs):
        return ScanResult(passed=False, reason="liquidity")

    def market_regime(self, *args, **kwargs):
        raise AssertionError("blocked scan must not classify a signal regime")

    def signal(self, *args, **kwargs):
        raise AssertionError("blocked scan must not reach signal()")


def test_evaluate_entry_stops_when_scan_is_blocked():
    scan, signal, regime = probe._evaluate_entry(
        "AAPL",
        None,
        _BlockedEngine(),
        {},
        None,
        None,
        None,
        None,
        None,
    )
    assert scan.passed is False
    assert signal is None
    assert regime is None


def test_advisor_note_runs_for_non_fired_signal(monkeypatch):
    seen = {}

    monkeypatch.setattr(probe, "build_advisor_context", lambda *args, **kwargs: SimpleNamespace(enabled=True))
    monkeypatch.setattr(
        probe,
        "advise_signal",
        lambda _ticker, signal, *args, **kwargs: seen.setdefault("signal", signal) and "advisor note",
    )
    signal = SignalResult(passed=False, reason="no entry conditions met")
    assert probe._advisor_note(
        "AAPL", signal, ScanResult(passed=True), pd.DataFrame(), {}, None, None, None,
    ) == "advisor note"
    assert seen["signal"] is signal


def test_dummy_signal_carries_scan_rejection_reason():
    signal = probe._dummy_signal(ScanResult(passed=False, reason="liquidity"))
    assert signal.passed is False
    assert signal.direction == "none" and signal.signal_type == "none"
    assert signal.reason == "scan blocked: liquidity"


def test_advisor_note_builds_a_read_only_context(monkeypatch):
    seen = {}

    def _build_context(_settings, *, read_only):
        seen["read_only"] = read_only
        return SimpleNamespace(enabled=True)

    monkeypatch.setattr(probe, "build_advisor_context", _build_context)
    monkeypatch.setattr(probe, "advise_signal", lambda *args, **kwargs: "advisor note")
    signal = SignalResult(
        passed=True,
        direction="long",
        signal_type="momentum",
        stop_price=95.0,
        target_price=110.0,
        min_rr=2.5,
    )
    df = pd.DataFrame({"close": [100.0], "ma_slow": [90.0]})
    assert probe._advisor_note(
        "AAPL", signal, ScanResult(passed=True), df, {}, None, None, None,
    ) == "advisor note"
    assert seen["read_only"] is True
