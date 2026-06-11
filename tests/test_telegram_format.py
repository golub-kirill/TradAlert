"""Formatter tests for src/core/telegram/format.py (no PTB, no network).

Content assertions run against de-tagged text so they survive HTML-styling
tweaks; raw-HTML checks are used only for escaping / structure (blockquote, no <pre>).
"""

from __future__ import annotations

import re
from datetime import date

from core.filter_engine import ScanResult, SignalResult
from core.position_manager import Position
from core.telegram import format as fmt
from core.types import TickerResult


def _plain(s: str) -> str:
    """Strip HTML tags so we can assert on the visible text."""
    return re.sub(r"<[^>]+>", "", s)


def _entry(direction="long", ticker="JNJ", close=232.77):
    s = SignalResult(
        passed=True, direction=direction, signal_type="momentum",
        stop_price=(93.10 if direction == "short" else 221.85),
        target_price=(76.20 if direction == "short" else 260.07),
        min_rr=2.5, size_mult=0.80, market_regime="BULL_NORMAL",
        ticker_trend="UPTREND", reason="entry signal fired",
        expected_hold_days=(10, 15),
    )
    return TickerResult(ticker, ScanResult(passed=True, close=close, atr=4.37, rsi=57.9), s)


def _exit(direction="exit_long", ticker="JNJ", close=239.10):
    s = SignalResult(passed=True, direction=direction, signal_type="momentum",
                     reason="MACD cross-down + BULL→CHOP")
    return TickerResult(ticker, ScanResult(passed=True, close=close), s)


# ── entry ────────────────────────────────────────────────────────────────────────

def test_entry_card_structure_and_content():
    out = fmt.format_entry(_entry(), risk_on=0.75, n_open=4)
    assert "<blockquote>" in out and "</blockquote>" in out
    assert "<pre>" not in out  # no grey copy-box
    assert len(out) <= fmt.CAPTION_LIMIT
    p = _plain(out)
    for token in ("📈", "JNJ", "LONG", "momentum"):
        assert token in p
    assert "entry 232.77" in p and "target 260.07" in p
    assert "stop 221.85" in p and "R:R 2.50" in p
    assert "hold 10" in p and "15d" in p and "0.80×" in p
    assert "BULL_NORMAL" in p and "risk-on 0.75" in p and "tailwind" in p
    assert "4 open" in p


def test_entry_escapes_ticker():
    out = fmt.format_entry(_entry(ticker="A&B"))
    assert "A&amp;B" in out
    assert "A&B" not in out  # raw ampersand never leaks into the HTML


def test_entry_checklist_renders_factor_line():
    out = _plain(fmt.format_entry(_entry(), checklist=[("TREND", True), ("LOC", None), ("RISK", False)]))
    assert "TREND ✅" in out and "LOC ▫️" in out and "RISK ❌" in out


def test_short_entry_borrow_and_tailwind():
    out = _plain(fmt.format_entry(_entry(direction="short", ticker="XYZ", close=88.40),
                                  borrow_pct=4.0, htb=True, risk_on=0.35))
    assert "📉" in out and "SHORT" in out
    assert "borrow 4.0%" in out and "HTB" in out
    assert "risk-on 0.35" in out and "tailwind" in out  # short + risk-off = tailwind


def test_tailwind_is_direction_aware():
    assert "headwind" in _plain(fmt.format_entry(_entry(direction="long"), risk_on=0.30))
    assert "tailwind" in _plain(fmt.format_entry(_entry(direction="short"), risk_on=0.30))
    assert "tailwind" in _plain(fmt.format_entry(_entry(direction="long"), risk_on=0.70))


def test_determinism():
    a = fmt.format_entry(_entry(), risk_on=0.6, n_open=2)
    b = fmt.format_entry(_entry(), risk_on=0.6, n_open=2)
    assert a == b


# ── exit / header / stand-down ───────────────────────────────────────────────────

def test_exit_render():
    out = _plain(fmt.format_exit(_exit(), entry_price=232.77, held_days=18,
                                 exit_price=239.10, realized_r=0.58, realized_pct=2.7))
    assert "⛔" in out and "EXIT LONG" in out
    assert "232.77 → 239.10" in out
    assert "realized +0.58R" in out and "(+2.7%)" in out
    assert "held 18d" in out and "MACD cross-down" in out


def test_cover_label():
    assert "COVER SHORT" in _plain(fmt.format_exit(_exit(direction="exit_short")))


def test_caption_cap_truncates():
    out = fmt.format_exit(_exit(), reason="x" * 4000)
    assert len(out) <= fmt.CAPTION_LIMIT


def test_daily_header_and_stand_down():
    d = date(2026, 6, 6)
    h = _plain(fmt.format_daily_header(d, n_entries=1, n_exits=0, n_shorts=0,
                                       regime_label="BULL_NORMAL", risk_on=0.75, n_open=4))
    assert "TradAlert · 2026-06-06" in h and "1 entry" in h and "0 exits" in h and "4 open" in h
    sd = _plain(fmt.format_stand_down(d, n_scanned=213, regime_label="CHOP_NORMAL",
                                      risk_on=0.41, n_open=4))
    assert "no actionable signals" in sd and "scanned 213" in sd and "4 open carried" in sd


# ── position card ────────────────────────────────────────────────────────────────

def test_position_card():
    pos = Position(id=12, ticker="JNJ", side="long", entry_price=232.77,
                   entry_date=date(2026, 5, 28), stop_price=221.85)
    out = fmt.format_position_card(
        pos, now=241.30, unrealized_r=0.78, unrealized_pct=3.7, days_held=9,
        to_target_r=1.7, to_stop_r=-1.0, time_stop_left=16, max_hold=25,
        mode="if_not_profit", engine_verdict="HOLD — no exit signal", risk_on=0.72)
    assert "&" not in out  # no raw ampersand anywhere in the HTML
    p = _plain(out)
    assert "JNJ #12" in p and "LONG" in p and "9d open" in p
    assert "PnL +0.78R" in p and "(+3.7%)" in p and "now 241.30" in p
    assert "→ tgt +1.70R" in p and "→ stop -1.00R" in p
    assert "time-stop 16d left (25d, if_not_profit)" in p
    assert "HOLD — no exit signal" in p and "risk-on 0.72" in p


# ── rich visuals (bars / gauges / pcts / expandable) ─────────────────────────────

def test_entry_rr_bar_and_expandable_detail():
    out = fmt.format_entry(_entry(), risk_on=0.75, n_open=4, checklist=[("TREND", True)])
    assert "<blockquote expandable>" in out          # secondary detail tucked away
    assert len(out) <= fmt.CAPTION_LIMIT
    p = _plain(out)
    assert "▰" in p and "▱" in p                      # R:R fill bar rendered


def test_entry_shows_move_pcts():
    p = _plain(fmt.format_entry(_entry()))
    assert "+11.7%" in p                              # entry 232.77 → target 260.07
    assert "-4.7%" in p                               # entry → stop 221.85 (downside)


def test_position_card_has_pnl_gauge():
    pos = Position(id=12, ticker="JNJ", side="long", entry_price=232.77,
                   entry_date=date(2026, 5, 28), stop_price=221.85)
    out = fmt.format_position_card(pos, now=241.30, unrealized_r=0.78,
                                   unrealized_pct=3.7, days_held=9)
    assert "&" not in out
    p = _plain(out)
    assert "●" in p and "🛑" in p and "🎯" in p        # gauge marker + flanks


def test_exit_winloss_emoji():
    assert "🟢" in _plain(fmt.format_exit(_exit(), realized_r=0.58, realized_pct=2.7))
    assert "🔴" in _plain(fmt.format_exit(_exit(), realized_r=-0.40, realized_pct=-1.9))


def test_meters_are_html_safe():
    # No meter/divider character ever introduces a raw ampersand into the HTML.
    out = fmt.format_entry(_entry(direction="short", ticker="X&Y", close=88.40),
                           risk_on=0.35, borrow_pct=4.0, htb=True,
                           checklist=[("TREND", False)])
    assert "X&amp;Y" in out and "X&Y" not in out
