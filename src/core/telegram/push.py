"""
Phase-1 outbound push: send the day's fired signals to Telegram after a scan.

`send_alerts(results, settings)` is the SYNC entry point called from main.py. It is
**fail-open** — a missing dependency, bad token, or Telegram outage degrades to a
log line and never breaks the scan or its exit code. With `telegram.enabled: false`
(the shipped default) it returns immediately, so the scan is byte-identical.

python-telegram-bot is imported lazily (inside `_send_all`) so this module — and
the formatters — import without PTB present.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date

from core.paths import SCREENSHOTS_DIR
from core.telegram import format as fmt
from core.telegram.config import TelegramConfig, load_telegram_config

logger = logging.getLogger(__name__)

# SignalResult.direction → (alert_type key, is_entry)
_DIRECTION_KIND = {
    "long": ("long_entry", True),
    "short": ("short_entry", True),
    "exit_long": ("exit_long", False),
    "exit_short": ("exit_short", False),
}


def send_alerts(results, settings, *, macro_state=None, run_date=None) -> None:
    """Select fired signals and push them. Never raises into the caller."""
    cfg = load_telegram_config(settings)
    if not cfg.enabled:
        return
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        logger.warning("[telegram] enabled but TG_BOT_TOKEN/TG_CHAT_ID missing — skipping push")
        return
    try:
        chat_id = int(chat)
    except (TypeError, ValueError):
        logger.warning("[telegram] TG_CHAT_ID must be the numeric chat id — skipping push")
        return

    selected = _select(results, cfg)
    if not selected and not cfg.send_stand_down:
        return

    risk_on = _safe_float(getattr(macro_state, "risk_on_score", None))
    n_open = _n_open()
    regime_label = (selected[0][0].signal.market_regime if selected else _any_regime(results)) or None
    rday = run_date or date.today()

    try:
        asyncio.run(_send_all(token, chat_id, cfg, selected, len(results),
                              risk_on, n_open, regime_label, rday))
    except Exception as exc:  # broad on purpose — alerting must never break the scan
        logger.warning("[telegram] push failed (scan unaffected) — %s", exc)


def send_notice(text: str, settings) -> None:
    """Send a one-off plain operator notice to the owner chat. Fail-open — never
    raises into the caller (used e.g. to flag a DB outage during a scan). No-op
    when telegram is disabled or the token/chat are unset.
    """
    cfg = load_telegram_config(settings)
    if not cfg.enabled:
        return
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        logger.warning("[telegram] notice skipped — TG_BOT_TOKEN/TG_CHAT_ID missing")
        return
    try:
        chat_id = int(chat)
    except (TypeError, ValueError):
        logger.warning("[telegram] notice skipped — TG_CHAT_ID not numeric")
        return
    try:
        asyncio.run(_send_notice(token, chat_id, cfg.parse_mode, text))
    except Exception as exc:  # alerting must never break the scan
        logger.warning("[telegram] notice failed (scan unaffected) — %s", exc)


async def _send_notice(token, chat_id, parse_mode, text):
    from core.telegram.bot import TelegramNotifier
    async with TelegramNotifier(token, chat_id, parse_mode=parse_mode) as nf:
        await nf.send_message(text)


# ── selection (pure) ─────────────────────────────────────────────────────────────

def _select(results, cfg: TelegramConfig):
    """Return [(TickerResult, kind)] for fired, non-watch-only, enabled, unmuted signals."""
    out = []
    muted = set(cfg.mute)
    for tr in results:
        s = getattr(tr, "signal", None)
        if s is None or not s.passed or getattr(s, "watch_only", False):
            continue
        kind_pair = _DIRECTION_KIND.get(s.direction)
        if kind_pair is None:
            continue
        kind, _is_entry = kind_pair
        if kind not in cfg.alert_types:
            continue
        if tr.ticker.upper() in muted:
            continue
        out.append((tr, kind))
    return out


# ── async send ───────────────────────────────────────────────────────────────────

async def _send_all(token, chat_id, cfg, selected, n_scanned, risk_on, n_open, regime_label, rday):
    from core.telegram.bot import TelegramNotifier

    async with TelegramNotifier(token, chat_id, parse_mode=cfg.parse_mode) as nf:
        if not selected:
            await nf.send_message(fmt.format_stand_down(
                rday, n_scanned=n_scanned, regime_label=regime_label,
                risk_on=risk_on, n_open=n_open))
            return

        n_long = sum(1 for _, k in selected if k == "long_entry")
        n_short = sum(1 for _, k in selected if k == "short_entry")
        n_exit = sum(1 for _, k in selected if k in ("exit_long", "exit_short"))
        await nf.send_message(fmt.format_daily_header(
            rday, n_entries=n_long, n_exits=n_exit, n_shorts=n_short,
            regime_label=regime_label, risk_on=risk_on, n_open=n_open))

        for tr, kind in selected:
            text, chart = _render(tr, kind, risk_on, n_open)
            markup = _markup(tr, kind, cfg)
            if chart is not None and not cfg.compact:
                await nf.send_photo(chart, caption=text, reply_markup=markup)
            else:
                await nf.send_message(text, reply_markup=markup)


def _render(tr, kind, risk_on, n_open):
    if kind in ("long_entry", "short_entry"):
        text = fmt.format_entry(tr, risk_on=risk_on, n_open=n_open,
                                checklist=_checklist(tr.signal) or None)
    else:
        text = fmt.format_exit(tr)
    return text, _latest_chart(tr.ticker)


# Telegram factor line: a per-group summary of the engine's entry-gate checks.
# Same source as the chart trigger panel (SignalResult.checks), so the two
# surfaces can never disagree with the real decision. Order is fixed for a
# stable read: TREND · MOM · LOC · VOL · RISK.
_GROUP_LABELS = (
    ("TREND", "TREND"),
    ("MOMENTUM", "MOM"),
    ("LOCATION", "LOC"),
    ("VOLATILITY", "VOL"),
    ("RISK", "RISK"),
)


def _checklist(signal):
    """[(label, state)] group marks from ``signal.checks``.

    state is True when every factor in the group passes, False when none do,
    None when mixed (rendered ✅ / ❌ / ▫️). Empty list when the signal carries
    no checks (``with_checks`` was off) → the factor line is omitted.
    """
    checks = getattr(signal, "checks", None) or []
    by_group: dict[str, list[bool]] = {}
    for c in checks:
        by_group.setdefault(c.group, []).append(bool(c.passed))
    out = []
    for group, label in _GROUP_LABELS:
        states = by_group.get(group)
        if not states:
            continue
        out.append((label, True if all(states) else False if not any(states) else None))
    return out


def _markup(tr, kind, cfg: TelegramConfig):
    # Buttons only when the daemon exists to answer them, and only on entries in P1.
    if not cfg.daemon_enabled or kind not in ("long_entry", "short_entry"):
        return None
    try:
        from core.telegram.keyboards import entry_actions
        s, sc = tr.signal, tr.scan
        side = "short" if s.direction == "short" else "long"
        return entry_actions(tr.ticker, float(sc.close), float(s.stop_price), side=side)
    except Exception:
        return None


# ── helpers ──────────────────────────────────────────────────────────────────────

def _latest_chart(ticker: str):
    """Newest screenshot for this ticker (today's), or None — avoids re-deriving the date stamp."""
    try:
        cands = sorted(SCREENSHOTS_DIR.glob(f"{ticker.upper()}_*.webp"),
                       key=lambda p: p.stat().st_mtime)
        return cands[-1] if cands else None
    except Exception:
        return None


def _n_open():
    try:
        from core import position_manager
        return len(position_manager.load_open_positions())
    except Exception:
        return None


def _any_regime(results):
    for tr in results:
        s = getattr(tr, "signal", None)
        if s is not None and getattr(s, "market_regime", ""):
            return s.market_regime
    return None


def _safe_float(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None
