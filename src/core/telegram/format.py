"""
Pure HTML formatters for Telegram messages (clean "card" style).

No python-telegram-bot, no network, no `now()` — deterministic given inputs, so
golden-string unit-testable and reusable by both the push (phase 1) and the
interactive daemon (phase 2). A `sendPhoto` message = chart `.webp` + the caption
these return: a bold emoji header over a `<blockquote>` card of emoji-labelled,
bold-valued lines (blockquote indents cleanly with no "copy" code-box).

Telegram constraints honored here:
    * caption ≤ 1024 chars, message ≤ 4096  (we cap captions at 1024)
    * allowed tags only: <b><i><u><s><code><pre><blockquote><a>  (no tables/colour)
    * every interpolated dynamic value is html-escaped
Inputs are duck-typed (TickerResult / SignalResult / ScanResult / Position), so
this module imports nothing from the engine.
"""

from __future__ import annotations

import html
from typing import Any, Sequence

CAPTION_LIMIT = 1024


# ── primitives ──────────────────────────────────────────────────────────────────

def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _b(value: Any) -> str:
    """Bold an already-safe scalar (numbers) for emphasis."""
    return f"<b>{value}</b>"


def _f2(x: Any) -> str:
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return "—"


def _r(x: float) -> str:
    return f"{float(x):+.2f}R"


def _pct(x: float) -> str:
    return f"{float(x):+.1f}%"


def _cap(msg: str, limit: int = CAPTION_LIMIT) -> str:
    if len(msg) <= limit:
        return msg
    # never truncate inside an open tag — close the blockquote cleanly
    cut = msg[: limit - 16].rsplit("\n", 1)[0]
    return cut + "\n…</blockquote>" if "<blockquote>" in cut else cut + "…"


def _card(header: str, lines: Sequence[str]) -> str:
    """Bold emoji header + a blockquote card of the body lines."""
    body = "\n".join(line for line in lines if line)
    return _cap(f"{header}\n<blockquote>{body}</blockquote>")


def _mark(state: Any) -> str:
    if state is True:
        return "✅"
    if state is False:
        return "❌"
    if state is None:
        return "▫️"
    return str(state)


def _factor_line(checklist: Sequence[tuple[str, Any]]) -> str:
    """'TREND ✅ · MOM ✅ · LOC ▫️ · …' from the engine gate results (one source of truth)."""
    return " · ".join(f"{_esc(lbl)} {_mark(state)}" for lbl, state in checklist)


def _regime_line(regime_label: str | None, risk_on: float | None, is_long: bool) -> str:
    """'🌐 BULL_NORMAL · risk-on 0.75 ✅ tailwind' — direction-aware tailwind/headwind."""
    parts: list[str] = []
    if regime_label:
        parts.append(_esc(regime_label))
    if risk_on is not None:
        tail = (risk_on > 0.5) == bool(is_long)
        parts.append(f"risk-on {risk_on:.2f} " + ("✅ tailwind" if tail else "⚠️ headwind"))
    return ("🌐 " + " · ".join(parts)) if parts else ""


# ── entry (long & short) ────────────────────────────────────────────────────────

def format_entry(tr: Any, *, risk_on: float | None = None, n_open: int | None = None,
                 checklist: Sequence[tuple[str, Any]] | None = None,
                 borrow_pct: float | None = None, htb: bool = False) -> str:
    """Clean card for a fired long/short entry. Attach the chart as the photo.

    `checklist` is the future entry-gate trigger panel; when present it renders the
    factor line. None today → omitted, so it lights up when that feature lands.
    """
    s, sc = tr.signal, tr.scan
    is_long = s.direction == "long"
    emoji = "📈" if is_long else "📉"
    side = "LONG" if is_long else "SHORT"
    lo, hi = s.expected_hold_days

    header = f"{emoji} <b>{_esc(tr.ticker)}</b> — {side} {_esc(s.signal_type)}"
    lines = [
        f"🎯 entry {_b(_f2(sc.close))} → target {_b(_f2(s.target_price))}",
        f"🛑 stop {_b(_f2(s.stop_price))}   ·   ⚖️ R:R {_b(f'{float(s.min_rr):.2f}')}",
        f"⏳ hold {lo}–{hi}d   ·   📦 size {float(s.size_mult):.2f}×",
    ]
    if checklist:
        lines.append("🔎 " + _factor_line(checklist))
    if not is_long and (borrow_pct is not None or htb):
        bits = []
        if borrow_pct is not None:
            bits.append(f"borrow {float(borrow_pct):.1f}%")
        if htb:
            bits.append("HTB")
        lines.append("🩳 " + " · ".join(bits))
    regime = _regime_line(s.market_regime, risk_on, is_long)
    if regime:
        lines.append(regime)
    if n_open is not None:
        lines.append(f"💼 {n_open} open")
    return _card(header, lines)


# ── exit / cover ────────────────────────────────────────────────────────────────

def format_exit(tr: Any, *, entry_price: float | None = None, held_days: int | None = None,
                exit_price: float | None = None, realized_r: float | None = None,
                realized_pct: float | None = None, reason: str | None = None) -> str:
    s, sc = tr.signal, tr.scan
    label = "COVER SHORT" if s.direction == "exit_short" else "EXIT LONG"
    header = f"⛔ <b>{_esc(tr.ticker)}</b> — {label}"
    px = exit_price if exit_price is not None else sc.close

    lines: list[str] = []
    if entry_price is not None and px is not None:
        lines.append(f"📉 {_f2(entry_price)} → {_b(_f2(px))}")
    elif px is not None:
        lines.append(f"📉 exit {_b(_f2(px))}")
    if realized_r is not None:
        line = f"💰 realized {_b(_r(realized_r))}"
        if realized_pct is not None:
            line += f" ({_pct(realized_pct)})"
        lines.append(line)
    if held_days is not None:
        lines.append(f"⏱ held {held_days}d")
    rsn = reason if reason is not None else getattr(s, "reason", "")
    if rsn:
        lines.append(f"📝 {_esc(rsn)}")
    return _card(header, lines) if lines else _cap(header)


# ── watch-only (no chart, quiet) ─────────────────────────────────────────────────

def format_watch_only(tr: Any, *, gate: float | int | None = None) -> str:
    s, sc = tr.signal, tr.scan
    side = "long" if s.direction in ("long", "exit_long") else "short"
    score_bit = ""
    if getattr(s, "score", 0):
        score_bit = f" · score {float(s.score):.0f}" + (f"/{gate} gate" if gate is not None else "")
    close_bit = f" · {_f2(sc.close)}" if sc.close is not None else ""
    return _cap(f"👀 <b>{_esc(tr.ticker)}</b> {side} watch{score_bit}{close_bit} — not alerting")


# ── daily header & stand-down ────────────────────────────────────────────────────

def format_daily_header(run_date: Any, *, n_entries: int = 0, n_exits: int = 0,
                        n_shorts: int = 0, regime_label: str | None = None,
                        risk_on: float | None = None, n_open: int | None = None) -> str:
    counts = [f"{n_entries} entr{'y' if n_entries == 1 else 'ies'}",
              f"{n_exits} exit{'' if n_exits == 1 else 's'}"]
    if n_shorts:
        counts.append(f"{n_shorts} short{'' if n_shorts == 1 else 's'}")
    lines = ["🟢 " + " · ".join(counts)]
    regime = _regime_line(regime_label, risk_on, True)
    if regime:
        lines.append(regime)
    if n_open is not None:
        lines.append(f"💼 {n_open} open")
    return _card(f"📊 <b>TradAlert</b> · {run_date:%Y-%m-%d}", lines)


def format_stand_down(run_date: Any, *, n_scanned: int = 0, regime_label: str | None = None,
                      risk_on: float | None = None, n_open: int | None = None) -> str:
    lines = [f"😴 no actionable signals · scanned {n_scanned}"]
    regime = _regime_line(regime_label, risk_on, True)
    if regime:
        lines.append(regime)
    if n_open is not None:
        lines.append(f"💼 {n_open} open carried")
    return _card(f"📊 <b>TradAlert</b> · {run_date:%Y-%m-%d}", lines)


# ── open-position card (phase 2 management) ──────────────────────────────────────

def format_position_card(pos: Any, *, now: float | None = None,
                         unrealized_r: float | None = None, unrealized_pct: float | None = None,
                         days_held: int | None = None, to_target_r: float | None = None,
                         to_stop_r: float | None = None, time_stop_left: int | None = None,
                         max_hold: int | None = None, mode: str | None = None,
                         engine_verdict: str | None = None, risk_on: float | None = None) -> str:
    held = f" · {days_held}d open" if days_held is not None else ""
    header = f"📊 <b>{_esc(pos.ticker)}</b> #{pos.id} · {str(pos.side).upper()}{held}"

    lines: list[str] = []
    if unrealized_r is not None:
        pl = f"💰 PnL {_b(_r(unrealized_r))}"
        if unrealized_pct is not None:
            pl += f" ({_pct(unrealized_pct)})"
        if now is not None:
            pl += f"  ·  now {_f2(now)}"
        lines.append(pl)
    else:
        base = f"entry {_f2(pos.entry_price)}"
        if now is not None:
            base += f"  ·  now {_f2(now)}"
        lines.append("💰 " + base)

    legs = []
    if to_target_r is not None:
        legs.append(f"🎯 → tgt {_r(to_target_r)}")
    if to_stop_r is not None:
        legs.append(f"🛑 → stop {_r(to_stop_r)}")
    if legs:
        lines.append("   ".join(legs))

    if time_stop_left is not None:
        ts = f"⏳ time-stop {time_stop_left}d left"
        if max_hold is not None:
            ts += f" ({max_hold}d" + (f", {_esc(mode)}" if mode else "") + ")"
        lines.append(ts)
    if engine_verdict:
        ev = f"🧭 {_esc(engine_verdict)}"
        if risk_on is not None:
            ev += f"  ·  risk-on {risk_on:.2f}"
        lines.append(ev)

    return _card(header, lines)
