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


def _exit_tr(ticker="KO", signal_type="momentum", direction="exit_long"):
    s = SignalResult(passed=True, direction=direction, signal_type=signal_type,
                     market_regime="CHOP_LOW", reason="regime flipped to CHOP — exit held long")
    return TickerResult(ticker, ScanResult(passed=True, close=50.0), s)


def test_split_regime_exits_advisory_pulls_only_regime_exits():
    selected = [
        (_tr("long", "JNJ"), "long_entry"),
        (_exit_tr("KO", "momentum"), "exit_long"),        # position-specific — stays
        (_exit_tr("ARX.TO", "regime"), "exit_long"),      # blanket regime flip — pulled
        (_exit_tr("EFA", "regime"), "exit_long"),
    ]
    kept, pulled = push._split_regime_exits(selected, "advisory")
    assert [tr.ticker for tr, _ in kept] == ["JNJ", "KO"]
    assert [tr.ticker for tr, _ in pulled] == ["ARX.TO", "EFA"]


def test_caution_message_splits_longs_and_shorts():
    caution = [
        (_exit_tr("EFA", "regime", "exit_long"), "exit_long"),
        (_exit_tr("TSLA", "regime", "exit_short"), "exit_short"),
    ]
    msg = push._caution_message(caution, "CHOP_LOW")
    assert "held long" in msg and "EFA" in msg
    assert "held short" in msg and "TSLA" in msg


def test_split_regime_exits_exit_mode_keeps_all():
    selected = [(_exit_tr("ARX.TO", "regime"), "exit_long")]
    kept, pulled = push._split_regime_exits(selected, "exit")
    assert len(kept) == 1 and pulled == []


def test_split_regime_exits_off_still_pulls():
    # "off" pulls them out of the card stream; the caller drops them (no caution).
    selected = [(_exit_tr("ARX.TO", "regime"), "exit_long")]
    kept, pulled = push._split_regime_exits(selected, "off")
    assert kept == [] and len(pulled) == 1


def test_advisory_sends_caution_when_only_regime_exits(monkeypatch):
    captured = {}

    async def fake(token, chat_id, cfg, selected, n_scanned, risk_on, n_open,
                   regime_label, rday, rejections=None, run_id=None, caution=None,
                   n_flagged=0, n_passed=None):
        captured["selected"] = selected
        captured["caution"] = caution
        captured["n_flagged"] = n_flagged
        captured["n_passed"] = n_passed

    monkeypatch.setattr(push, "_send_all", fake)
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "12345")
    from core import position_manager
    monkeypatch.setattr(position_manager, "load_open_positions", lambda: {})
    # Only regime-flip exits fired → selected empties out, caution carries them.
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED)
    assert captured["selected"] == []
    assert [tr.ticker for tr, _ in captured["caution"]] == ["ARX.TO"]


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


# ── _render: NEEDS_REVIEW banner + caption-length guard ──────────────────────

def _entry_tr(ticker="JNJ", tier="LIVE", review_reason=""):
    s = SignalResult(passed=True, direction="long", signal_type="momentum",
                     stop_price=221.85, target_price=260.07, min_rr=2.5, size_mult=0.8,
                     market_regime="BULL_NORMAL", reason="x",
                     expected_hold_days=(10, 15), tier=tier, review_reason=review_reason)
    return TickerResult(ticker, ScanResult(passed=True, close=232.77), s)


def test_render_prepends_needs_review_banner_html_escaped(monkeypatch):
    monkeypatch.setattr(push, "_latest_chart", lambda t: None)
    monkeypatch.setattr(push.fmt, "format_entry", lambda tr, **k: "BODY")
    tr = _entry_tr(tier="NEEDS_REVIEW", review_reason="gap 2.3×ATR & <stale>")
    text, _ = push._render(tr, "long_entry", risk_on=None, n_open=0)
    assert text.startswith("⚠ <b>NEEDS REVIEW</b>")
    assert "BODY" in text
    assert "&lt;stale&gt;" in text and "&amp;" in text  # review_reason escaped


def test_render_no_banner_for_clean_live_entry(monkeypatch):
    monkeypatch.setattr(push, "_latest_chart", lambda t: None)
    monkeypatch.setattr(push.fmt, "format_entry", lambda tr, **k: "BODY")
    text, _ = push._render(_entry_tr(tier="LIVE"), "long_entry", risk_on=None, n_open=0)
    assert "NEEDS REVIEW" not in text


def test_render_drops_chart_when_caption_would_overflow(monkeypatch):
    monkeypatch.setattr(push, "_latest_chart", lambda t: "fake/chart.webp")
    monkeypatch.setattr(push.fmt, "format_entry", lambda tr, **k: "x" * 1100)
    tr = _entry_tr(tier="NEEDS_REVIEW", review_reason="stale 1 session")
    text, chart = push._render(tr, "long_entry", risk_on=None, n_open=0)
    assert len(text) > push._CAPTION_LIMIT
    assert chart is None  # dropped → sent as a full message, never truncated


def test_render_keeps_chart_within_caption_limit(monkeypatch):
    monkeypatch.setattr(push, "_latest_chart", lambda t: "fake/chart.webp")
    monkeypatch.setattr(push.fmt, "format_entry", lambda tr, **k: "short body")
    text, chart = push._render(_entry_tr(tier="LIVE"), "long_entry", risk_on=None, n_open=0)
    assert chart == "fake/chart.webp"


# ── regime-caution dedup + weakening split ────────────────────────────────────


def _regime_exit_tr(ticker, ticker_trend="", macd_hist=None):
    s = SignalResult(passed=True, direction="exit_long", signal_type="regime",
                     market_regime="CHOP_LOW", ticker_trend=ticker_trend,
                     reason="regime flipped — exit held long")
    return TickerResult(ticker, ScanResult(passed=True, close=50.0,
                                           macd_hist=macd_hist), s)


def test_is_repeat_advisory_matrix():
    # State = (delivered_set, bull_run_since_delivery) or None.
    # Never delivered / DB error / table missing → send.
    assert push._is_repeat_advisory(None, {"KO"}) is False
    # Same episode, delivered superset → suppress (shrinking set too).
    assert push._is_repeat_advisory(({"KO", "BA"}, False), {"KO", "BA"}) is True
    assert push._is_repeat_advisory(({"KO", "BA"}, False), {"KO"}) is True
    # NEW name entered the advisory set → send.
    assert push._is_repeat_advisory(({"KO"}, False), {"KO", "NVDA"}) is False
    # A BULL run since the delivery = new episode → send even for the same set.
    assert push._is_repeat_advisory(({"KO", "BA"}, True), {"KO", "BA"}) is False
    # Degenerate: empty delivered set / empty current set → send / no-op.
    assert push._is_repeat_advisory((set(), False), {"KO"}) is False
    assert push._is_repeat_advisory(({"KO"}, False), set()) is False


def test_caution_delivery_recorded_only_on_successful_send(monkeypatch):
    """A failed push must leave NO delivery record (so the next scan retries);
    a successful one must record exactly the advised set."""
    recorded = []
    monkeypatch.setattr(push, "_record_caution",
                        lambda run_id, tickers: recorded.append((run_id, set(tickers))))
    monkeypatch.setattr(push, "_caution_state", lambda: None)   # never delivered → send
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "12345")
    from core import position_manager
    monkeypatch.setattr(position_manager, "load_open_positions", lambda: {})

    async def ok(*a, **k):
        return True

    monkeypatch.setattr(push, "_send_all", ok)
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED, run_id=7)
    assert recorded == [(7, {"ARX.TO"})]

    recorded.clear()

    async def boom(*a, **k):
        raise RuntimeError("telegram outage")

    monkeypatch.setattr(push, "_send_all", boom)
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED, run_id=8)  # must not raise
    assert recorded == []


def test_failed_send_lets_the_next_scan_through(monkeypatch):
    """End-to-end over a fake delivery journal: an episode whose FIRST push fails
    must still reach the reader on the next scan — the failure mode a fail-open
    dedup must not have (the journal alone would have suppressed it forever)."""
    journal = []
    monkeypatch.setattr(push, "_record_caution",
                        lambda run_id, tickers: journal.append(set(tickers)))
    monkeypatch.setattr(push, "_caution_state",
                        lambda: (journal[-1], False) if journal else None)
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "12345")
    from core import position_manager
    monkeypatch.setattr(position_manager, "load_open_positions", lambda: {})
    sends = []

    async def outage(*a, **k):
        sends.append("failed")
        raise RuntimeError("telegram outage")

    async def ok(*a, **k):
        sends.append("sent")
        return True

    # Scan 1 — episode starts, push fails: nothing journaled.
    monkeypatch.setattr(push, "_send_all", outage)
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED, run_id=20)
    assert journal == []

    # Scan 2 — same episode, same set: must RETRY, not suppress.
    monkeypatch.setattr(push, "_send_all", ok)
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED, run_id=21)
    assert journal == [{"ARX.TO"}]

    # Scan 3 — now genuinely delivered, so the repeat is suppressed.
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED, run_id=22)
    assert sends == ["failed", "sent"]
    assert journal == [{"ARX.TO"}]


def test_suppressed_caution_is_not_recorded(monkeypatch):
    """Suppression must not write a fresh delivery row — only real sends do."""
    recorded = []
    monkeypatch.setattr(push, "_record_caution",
                        lambda run_id, tickers: recorded.append(run_id))
    monkeypatch.setattr(push, "_caution_state", lambda: ({"ARX.TO"}, False))
    monkeypatch.setenv("TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT_ID", "12345")
    from core import position_manager
    monkeypatch.setattr(position_manager, "load_open_positions", lambda: {})
    sent = []

    async def fake(*a, **k):
        sent.append(1)
        return True

    monkeypatch.setattr(push, "_send_all", fake)
    push.send_alerts([_exit_tr("ARX.TO", "regime")], _ENABLED, run_id=9)
    assert recorded == []          # nothing recorded…
    assert sent == []              # …because nothing needed sending at all


def test_is_weakening_reads_own_trend_then_macd():
    assert push._is_weakening(_regime_exit_tr("BA", "CHOP")) is True         # trend broken
    assert push._is_weakening(_regime_exit_tr("GS", "UPTREND", -0.4)) is True   # MACD down
    assert push._is_weakening(_regime_exit_tr("CVX", "UPTREND", 0.6)) is False  # healthy
    assert push._is_weakening(_regime_exit_tr("XX", "", None)) is False      # missing → healthy


def test_caution_message_groups_weakening():
    caution = [
        (_regime_exit_tr("BA", "CHOP"), "exit_long"),
        (_regime_exit_tr("CVX", "UPTREND", 0.6), "exit_long"),
    ]
    msg = push._caution_message(caution, "CHOP_LOW")
    assert "weakening" in msg and "BA" in msg
    assert "still trending" in msg and "CVX" in msg
