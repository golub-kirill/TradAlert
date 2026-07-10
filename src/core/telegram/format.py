"""
Pure HTML formatters for Telegram messages (rich "card" style).

No python-telegram-bot, no network, no `now()` — deterministic given inputs, so
golden-string unit-testable and reusable by both the outbound push and the
interactive daemon. A `sendPhoto` message = chart `.webp` + the caption
these return: a bold emoji header over a `<blockquote>` card, plus unicode meters
(▰▱ R:R bar, ●-marker PnL gauge), profit/risk %s, and a collapsible
`<blockquote expandable>` detail block so the headline stays compact.

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

# An R:R of this fills the bar; the PnL gauge maps [LO, HI] R onto its width.
_RR_FULL = 4.0
_PNL_LO, _PNL_HI = -1.5, 3.0


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
    # never truncate inside an open tag — close any open blockquote cleanly
    cut = msg[: limit - 20].rsplit("\n", 1)[0]
    if cut.count("<blockquote") > cut.count("</blockquote>"):
        return cut + "\n…</blockquote>"
    return cut + "…"


def _card(header: str, lines: Sequence[str]) -> str:
    """Bold emoji header + a blockquote card of the body lines."""
    body = "\n".join(line for line in lines if line)
    return _cap(f"{header}\n<blockquote>{body}</blockquote>")


def _card2(header: str, primary: Sequence[str], detail: Sequence[str]) -> str:
    """Header + a primary blockquote + a collapsible expandable detail blockquote.

    The headline (price / R:R) stays visible; secondary context (hold, size,
    factor line, regime, open count) tucks into a one-tap expandable quote.
    """
    body = "\n".join(line for line in primary if line)
    out = f"{header}\n<blockquote>{body}</blockquote>"
    det = "\n".join(line for line in detail if line)
    if det:
        out += f"\n<blockquote expandable>{det}</blockquote>"
    return _cap(out)


# ── unicode meters (visible text, so safe under tag-stripping tests) ──────────────

def _bar(frac: Any, width: int = 8) -> str:
    """A ▰▱ progress bar for a [0,1] fraction."""
    try:
        frac = max(0.0, min(1.0, float(frac)))
    except (TypeError, ValueError):
        return ""
    n = int(round(frac * width))
    return "▰" * n + "▱" * (width - n)


def _gauge(frac: Any, width: int = 9) -> str:
    """A ▱ track with a ● marker at the [0,1] position (clamped)."""
    try:
        frac = max(0.0, min(1.0, float(frac)))
    except (TypeError, ValueError):
        return ""
    pos = int(round(frac * (width - 1)))
    return "▱" * pos + "●" + "▱" * (width - 1 - pos)


def _rr_bar(rr: Any) -> str:
    """R:R as a fill bar (R:R of _RR_FULL fills it)."""
    try:
        return _bar(float(rr) / _RR_FULL)
    except (TypeError, ValueError):
        return ""


def _pnl_frac(r: Any) -> float | None:
    """Map an R value onto the [_PNL_LO, _PNL_HI] gauge track → [0,1]."""
    try:
        return (float(r) - _PNL_LO) / (_PNL_HI - _PNL_LO)
    except (TypeError, ValueError):
        return None


def _move_pct(frm: Any, to: Any, sign: int, *, loss: bool = False) -> str:
    """Signed % move from `frm` to `to`, oriented so profit reads positive.

    `sign` = +1 long / -1 short. `loss=True` forces the magnitude negative
    (used for the entry→stop distance, which is the downside if hit).
    """
    try:
        frm, to = float(frm), float(to)
        if frm == 0:
            return ""
        pct = sign * (to - frm) / frm * 100.0
    except (TypeError, ValueError):
        return ""
    if loss:
        pct = -abs(pct)
    return f"{pct:+.1f}%"


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
                 panel: tuple[Sequence[Any], Sequence[Any]] | None = None,
                 borrow_pct: float | None = None, htb: bool = False) -> str:
    """Clean card for a fired long/short entry. Attach the chart as the photo.

    Headline: entry→target (with upside %), stop (with downside %), and an R:R
    fill bar. Secondary detail (hold, size, decisive/advisory panel, regime, open
    count) goes in an expandable blockquote. `panel` is ``(decisive, advisory)`` —
    the gates that fired vs non-gating context — kept separate so the card is not
    read as a broad multi-factor score (audit S7).
    """
    s, sc = tr.signal, tr.scan
    is_long = s.direction == "long"
    emoji = "📈" if is_long else "📉"
    side = "LONG" if is_long else "SHORT"
    sign = 1 if is_long else -1
    lo, hi = s.expected_hold_days

    header = f"{emoji} <b>{_esc(tr.ticker)}</b> — {side} {_esc(s.signal_type)}"

    tgt_line = f"🎯 entry {_b(_f2(sc.close))} → target {_b(_f2(s.target_price))}"
    up = _move_pct(sc.close, s.target_price, sign)
    if up:
        tgt_line += f"  <i>{up}</i>"

    stop_line = f"🛑 stop {_b(_f2(s.stop_price))}"
    dn = _move_pct(sc.close, s.stop_price, sign, loss=True)
    if dn:
        stop_line += f"  <i>{dn}</i>"
    stop_line += f"   ·   ⚖️ R:R {_b(f'{float(s.min_rr):.2f}')}"
    rr_bar = _rr_bar(s.min_rr)
    if rr_bar:
        stop_line += f"  {rr_bar}"

    primary = [tgt_line, stop_line]

    detail = [f"⏳ hold {lo}–{hi}d   ·   📦 size {float(s.size_mult):.2f}×"]
    event_risk = getattr(s, "event_risk", "")
    if event_risk:
        detail.append(f"🗓 event risk: {_esc(event_risk)}")
    advisor_note = getattr(s, "advisor_note", "")
    if advisor_note:
        detail.append(f"🤖 {_esc(advisor_note)}")
    if panel:
        decisive, advisory = panel
        if decisive:
            detail.append("🔎 fired on · " + " · ".join(
                f"{_esc(n)} {_esc(d)}" for n, d in decisive))
        if advisory:
            detail.append("ℹ️ advisory · " + " · ".join(
                f"{_esc(n)} {_esc(d)}" for n, d in advisory))
    if not is_long and (borrow_pct is not None or htb):
        bits = []
        if borrow_pct is not None:
            bits.append(f"borrow {float(borrow_pct):.1f}%")
        if htb:
            bits.append("HTB")
        detail.append("🩳 " + " · ".join(bits))
    regime = _regime_line(s.market_regime, risk_on, is_long)
    if regime:
        detail.append(regime)
    if n_open is not None:
        detail.append(f"💼 {n_open} open")

    return _card2(header, primary, detail)


# ── exit / cover ────────────────────────────────────────────────────────────────

def format_exit(tr: Any, *, entry_price: float | None = None, held_days: int | None = None,
                exit_price: float | None = None, realized_r: float | None = None,
                realized_pct: float | None = None, reason: str | None = None) -> str:
    s, sc = tr.signal, tr.scan
    label = "COVER SHORT" if s.direction == "exit_short" else "EXIT LONG"
    win = ""
    if realized_r is not None:
        try:
            win = "  🟢" if float(realized_r) >= 0 else "  🔴"
        except (TypeError, ValueError):
            win = ""
    header = f"⛔ <b>{_esc(tr.ticker)}</b> — {label}{win}"
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
        frac = _pnl_frac(realized_r)
        if frac is not None:
            lines.append(f"🔴 {_gauge(frac)} 🟢")
    if held_days is not None:
        lines.append(f"⏱ held {held_days}d")
    rsn = reason if reason is not None else getattr(s, "reason", "")
    if rsn:
        lines.append(f"📝 {_esc(rsn)}")
    return _card(header, lines) if lines else _cap(header)


def format_regime_caution(tickers: Sequence[str], *, regime_label: str | None = None) -> str:
    """One consolidated caution for a broad regime-flip exit across held longs.

    Replaces the flood of individual EXIT cards the flip would otherwise emit: it
    lists the affected positions and states plainly that nothing is auto-closed —
    the trader reviews and decides. Empty ``tickers`` → "" (caller skips the send).
    """
    names = [str(t).upper() for t in tickers if t]
    if not names:
        return ""
    n = len(names)
    where = f" ({_esc(regime_label)})" if regime_label else ""
    lines = [
        f"Regime turned non-BULL{where} — {n} held long{'' if n == 1 else 's'} "
        "flagged for review.",
        "🔎 " + ", ".join(_esc(t) for t in names),
        "ℹ️ Not auto-closed — trim or hold at your discretion.",
    ]
    return _card("⚠️ <b>REGIME CAUTION</b>", lines)


# ── daily header & stand-down ────────────────────────────────────────────────────

def format_daily_header(run_date: Any, *, n_entries: int = 0, n_exits: int = 0,
                        n_shorts: int = 0, regime_label: str | None = None,
                        risk_on: float | None = None, n_open: int | None = None) -> str:
    counts = [f"🟢 {n_entries} entr{'y' if n_entries == 1 else 'ies'}",
              f"⛔ {n_exits} exit{'' if n_exits == 1 else 's'}"]
    if n_shorts:
        counts.append(f"🩳 {n_shorts} short{'' if n_shorts == 1 else 's'}")
    lines = [" · ".join(counts)]
    regime = _regime_line(regime_label, risk_on, True)
    if regime:
        lines.append(regime)
    if n_open is not None:
        lines.append(f"💼 {n_open} open")
    return _card(f"📊 <b>TradAlert</b> · {run_date:%Y-%m-%d}", lines)


def format_stand_down(run_date: Any, *, n_scanned: int = 0, regime_label: str | None = None,
                      risk_on: float | None = None, n_open: int | None = None,
                      rejections: Sequence[Any] | None = None) -> str:
    lines = [f"😴 no actionable signals · scanned {n_scanned}"]
    regime = _regime_line(regime_label, risk_on, True)
    if regime:
        lines.append(regime)
    if n_open is not None:
        lines.append(f"💼 {n_open} open carried")
    block_line = _rejections_line(rejections)
    if block_line:
        lines.append(block_line)
    return _card(f"📊 <b>TradAlert</b> · {run_date:%Y-%m-%d}", lines)


def _rejections_line(rejections: Sequence[Any] | None, *, top: int = 5) -> str:
    """One compact, HTML-escaped 'Top blocks: gate ×n · …' line, or '' when empty.

    Accepts ``{"gate","n"}`` dicts or ``(gate, n)`` tuples. Escapes each gate.
    """
    if not rejections:
        return ""
    parts: list[str] = []
    for item in list(rejections)[:top]:
        if isinstance(item, dict):
            gate, n = item.get("gate"), item.get("n")
        else:
            gate, n = item[0], item[1]
        parts.append(f"{_esc(gate)} ×{n}")
    return ("🚧 Top blocks: " + " · ".join(parts)) if parts else ""


# ── open-position card (daemon management) ───────────────────────────────────────

def format_position_card(pos: Any, *, now: float | None = None,
                         unrealized_r: float | None = None, unrealized_pct: float | None = None,
                         days_held: int | None = None, to_target_r: float | None = None,
                         to_stop_r: float | None = None, time_stop_left: int | None = None,
                         max_hold: int | None = None, mode: str | None = None,
                         engine_verdict: str | None = None, risk_on: float | None = None,
                         remaining_frac: float | None = None, closed: bool = False) -> str:
    if days_held is not None:
        held = f" · {days_held}d {'held' if closed else 'open'}"
    else:
        held = " · closed" if closed else ""
    header = f"📊 <b>{_esc(pos.ticker)}</b> #{pos.id} · {str(pos.side).upper()}{held}"

    lines: list[str] = []
    if unrealized_r is not None:
        pl = f"💰 {'realized' if closed else 'PnL'} {_b(_r(unrealized_r))}"
        if unrealized_pct is not None:
            pl += f" ({_pct(unrealized_pct)})"
        if now is not None:
            pl += f"  ·  now {_f2(now)}"
        lines.append(pl)
        frac = _pnl_frac(unrealized_r)
        if frac is not None:
            lines.append(f"🛑 {_gauge(frac)} 🎯")
    else:
        base = f"entry {_f2(pos.entry_price)}"
        if unrealized_pct is not None:           # realized/unrealized % even when R is undefined
            base += f" ({_pct(unrealized_pct)})"
        if now is not None:
            base += f"  ·  now {_f2(now)}"
        lines.append("💰 " + base)

    # A partly scaled-out position: show how much remains open (manual ½/⅓ closes).
    if remaining_frac is not None and remaining_frac < 0.999:
        scaled = max(0.0, 1.0 - remaining_frac)
        lines.append(f"✂️ scaled {scaled:.0%} out · {remaining_frac:.0%} open")

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


# ── compact positions digest (one-message dashboard) ─────────────────────────────

def _position_row(row: Any) -> str:
    """One aligned line for a position in the compact table.

    `row` is a plain dict (ticker/id/side + optional unrealized_r, unrealized_pct,
    to_stop_r, days_held). Missing live figures degrade to a bare identity line — a
    position whose metrics couldn't be computed still lists.
    """
    side = str(row.get("side", "?")).upper()[:1] or "?"
    parts = []
    r = row.get("unrealized_r")
    if r is not None:
        try:
            emoji = "🟢" if float(r) >= 0 else "🔴"
        except (TypeError, ValueError):
            emoji = "⚪"
    else:
        emoji = "⚪"
    parts.append(f"{emoji} {_esc(row.get('ticker', '?'))} #{row.get('id', '?')} {side}")
    if r is not None:
        seg = _r(r)
        pct = row.get("unrealized_pct")
        if pct is not None:
            seg += f" ({_pct(pct)})"
        parts.append(seg)
    to_stop = row.get("to_stop_r")
    if to_stop is not None:
        parts.append(f"→stop {_r(to_stop)}")
    dh = row.get("days_held")
    if dh is not None:
        parts.append(f"{dh}d")
    return "  ".join(parts)


def format_positions_table(rows: Sequence[Any], *, budget_note: str | None = None,
                           realized_note: str | None = None) -> str:
    """Compact one-message digest of all open positions (read-only dashboard).

    Each `row` is a plain dict so this imports nothing from the engine. One line per
    position, then an aggregate footer from the budget / realized notes. If the rows
    would overflow the caption, they collapse to a '…and N more · /pos ID' tail so
    the message never exceeds Telegram's cap.
    """
    if not rows:
        return _card("📋 <b>Positions</b>", ["💼 no open positions"])
    footer = [n for n in (budget_note, realized_note) if n]
    # Leave headroom for the header, blockquote tags, and the footer.
    room = CAPTION_LIMIT - 120 - sum(len(f) + 1 for f in footer)
    body: list[str] = []
    used = 0
    for i, row in enumerate(rows):
        line = _position_row(row)
        if used + len(line) + 1 > room:
            body.append(f"…and {len(rows) - i} more · /pos ID")
            break
        body.append(line)
        used += len(line) + 1
    if footer:
        body.append("· · ·")
        body.extend(_esc(f) for f in footer)
    return _card(f"📋 <b>Positions</b> · {len(rows)} open", body)
