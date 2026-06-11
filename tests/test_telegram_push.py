"""Push bridge: selection + fail-open + enable gating (no network; _send_all mocked)."""

from __future__ import annotations

from core.filter_engine import ScanResult, SignalResult
from core.telegram import push
from core.telegram.config import load_telegram_config
from core.types import TickerResult


def _tr(direction="long", ticker="JNJ"):
    s = SignalResult(passed=True, direction=direction, signal_type="momentum",
                     stop_price=221.85, target_price=260.07, min_rr=2.5, size_mult=0.8,
                     market_regime="BULL_NORMAL", reason="x",
                     expected_hold_days=(10, 15))
    return TickerResult(ticker, ScanResult(passed=True, close=232.77), s)


def _results():
    return [_tr("long", "JNJ"), _tr("exit_long", "KO"), _tr("long", "TSLA")]


_ENABLED = {"telegram": {"enabled": True, "mute": ["TSLA"]}}


def test_select_excludes_muted():
    sel = push._select(_results(), load_telegram_config(_ENABLED))
    tickers = [tr.ticker for tr, _ in sel]
    kinds = [k for _, k in sel]
    assert "JNJ" in tickers and "KO" in tickers
    assert "TSLA" not in tickers  # muted excluded
    assert "long_entry" in kinds and "exit_long" in kinds


def test_disabled_does_not_send(monkeypatch):
    calls = []

    async def fake(*a, **k):
        calls.append(1)

    monkeypatch.setattr(push, "_send_all", fake)
    push.send_alerts(_results(), {"telegram": {"enabled": False}})
    assert calls == []


def test_missing_token_does_not_send(monkeypatch):
    calls = []

    async def fake(*a, **k):
        calls.append(1)

    monkeypatch.setattr(push, "_send_all", fake)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    push.send_alerts(_results(), _ENABLED)
    assert calls == []


def test_non_numeric_chat_id_does_not_send(monkeypatch):
    calls = []

    async def fake(*a, **k):
        calls.append(1)

    monkeypatch.setattr(push, "_send_all", fake)
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "not-a-number")
    push.send_alerts(_results(), _ENABLED)
    assert calls == []


def test_enabled_sends(monkeypatch):
    calls = []

    async def fake(*a, **k):
        calls.append((a, k))

    monkeypatch.setattr(push, "_send_all", fake)
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "12345")
    from core import position_manager
    monkeypatch.setattr(position_manager, "load_open_positions", lambda: {})
    push.send_alerts(_results(), _ENABLED)
    assert len(calls) == 1


def test_fail_open_swallows_send_errors(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(push, "_send_all", boom)
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "12345")
    from core import position_manager
    monkeypatch.setattr(position_manager, "load_open_positions", lambda: {})
    push.send_alerts(_results(), _ENABLED)  # must NOT raise
