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
import html
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


def send_alerts(results, settings, *, macro_state=None, run_date=None, stand_down=None,
                run_id=None) -> None:
    """Select fired signals and push them. Never raises into the caller.

    `stand_down` is the optional DB-backed rejection rollup from
    persistence.db.stand_down_summary (or None); it enriches the stand-down
    message's "Top blocks" line and is ignored when signals fired. `run_id` (the
    scan's id) rides into each entry card's "🚫 Skip" button so a skipped fire can
    be journaled for opportunity_tracker.
    """
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
    # A broad regime-flip exit fires on every held long at once; unless mode="exit",
    # pull those out so they collapse into a single caution instead of a wall of
    # EXIT cards (position-specific exits stay in `selected` and fire normally).
    selected, regime_exits = _split_regime_exits(selected, cfg.regime_flip_exit_mode)
    caution = regime_exits if cfg.regime_flip_exit_mode == "advisory" else []
    if not selected and not caution and not cfg.send_stand_down:
        return

    risk_on = _safe_float(getattr(macro_state, "risk_on_score", None))
    n_open = _n_open()
    _first = selected or caution
    regime_label = (_first[0][0].signal.market_regime if _first else _any_regime(results)) or None
    rday = run_date or date.today()

    rejections = (stand_down or {}).get("rejection_gates") or None

    try:
        asyncio.run(_send_all(token, chat_id, cfg, selected, len(results),
                              risk_on, n_open, regime_label, rday, rejections, run_id,
                              caution))
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
    """Return [(TickerResult, kind)] for fired, enabled, unmuted signals."""
    out = []
    muted = set(cfg.mute)
    for tr in results:
        s = getattr(tr, "signal", None)
        if s is None or not s.passed:
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


def _split_regime_exits(selected, mode: str):
    """Partition regime-flip exits out of ``selected`` per ``mode``.

    Returns ``(kept, pulled)``. A regime-flip exit is an exit whose
    ``signal_type == "regime"`` (the blanket "regime flipped — exit held long/short"
    signal). In "exit" mode nothing is pulled (legacy per-position EXIT cards).
    """
    if mode == "exit":
        return selected, []
    kept, pulled = [], []
    for tr, kind in selected:
        s = getattr(tr, "signal", None)
        if kind in ("exit_long", "exit_short") and getattr(s, "signal_type", "") == "regime":
            pulled.append((tr, kind))
        else:
            kept.append((tr, kind))
    return kept, pulled


# ── async send ───────────────────────────────────────────────────────────────────

async def _send_all(token, chat_id, cfg, selected, n_scanned, risk_on, n_open, regime_label, rday,
                    rejections=None, run_id=None, caution=None):
    from core.telegram.bot import TelegramNotifier

    caution = caution or []

    async with TelegramNotifier(token, chat_id, parse_mode=cfg.parse_mode) as nf:
        if not selected:
            if caution:
                # Nothing else fired — send the regime caution on its own.
                await nf.send_message(fmt.format_regime_caution(
                    [tr.ticker for tr, _ in caution], regime_label=regime_label))
            else:
                await nf.send_message(fmt.format_stand_down(
                    rday, n_scanned=n_scanned, regime_label=regime_label,
                    risk_on=risk_on, n_open=n_open, rejections=rejections))
            return

        n_long = sum(1 for _, k in selected if k == "long_entry")
        n_short = sum(1 for _, k in selected if k == "short_entry")
        n_exit = sum(1 for _, k in selected if k in ("exit_long", "exit_short"))
        await nf.send_message(fmt.format_daily_header(
            rday, n_entries=n_long, n_exits=n_exit, n_shorts=n_short,
            regime_label=regime_label, risk_on=risk_on, n_open=n_open))

        for tr, kind in selected:
            text, chart = _render(tr, kind, risk_on, n_open)
            markup = _markup(tr, kind, cfg, run_id)
            if chart is not None and not cfg.compact:
                await nf.send_photo(chart, caption=text, reply_markup=markup)
            else:
                await nf.send_message(text, reply_markup=markup)

        if caution:
            # One consolidated caution after the real cards, not N EXIT directives.
            await nf.send_message(fmt.format_regime_caution(
                [tr.ticker for tr, _ in caution], regime_label=regime_label))


# Telegram caps a photo CAPTION at 1024 chars (a plain message allows 4096).
_CAPTION_LIMIT = 1024


def _render(tr, kind, risk_on, n_open):
    chart = _latest_chart(tr.ticker)
    if kind in ("long_entry", "short_entry"):
        text = fmt.format_entry(tr, risk_on=risk_on, n_open=n_open,
                                panel=_panel(tr.signal))
        # Data-freshness tier: a stale-after-refetch or gapped entry is flagged, not sent as
        # a clean LIVE alert (main.py sets it; default "LIVE" → unchanged for normal fires).
        if getattr(tr.signal, "tier", "LIVE") == "NEEDS_REVIEW":
            reason = html.escape(getattr(tr.signal, "review_reason", "") or "data freshness")
            text = f"⚠ <b>NEEDS REVIEW</b> — {reason}\n{text}"
    else:
        text = fmt.format_exit(tr)
    # If the (banner + body) text would overflow a photo caption, drop the chart
    # so the alert goes out as a full message instead of being truncated mid-HTML
    # — a too-long caption is rejected by Telegram and the alert would be lost.
    if chart is not None and len(text) > _CAPTION_LIMIT:
        logger.warning(
            "[telegram] %s alert text %d chars > %d caption limit — sending "
            "without chart to avoid truncation.", tr.ticker, len(text), _CAPTION_LIMIT)
        chart = None
    return text, chart


# Entry-card panel (audit S7): split the engine's gate checks into what DECIDED the
# signal (the MOMENTUM entry gates) vs non-gating ADVISORY context (52-week position),
# so the card no longer reads as a broad multi-factor "score". Same source as the chart
# panel (SignalResult.checks); event_risk is surfaced separately by format_entry.
def _panel(signal):
    """``(decisive, advisory)`` rows for the entry card — each ``[(name, detail)]``.

    decisive = the MOMENTUM gates that actually fired the signal; advisory = the
    52-week position (context, never gating). Empty lists when the signal has no
    checks (``with_checks`` was off) → both lines are omitted.
    """
    checks = getattr(signal, "checks", None) or []
    decisive = [(c.name, c.detail) for c in checks if c.group == "MOMENTUM"]
    advisory = [(c.name, c.detail) for c in checks
                if c.group == "LOCATION" and c.name == "52W pos"]
    return decisive, advisory


def _markup(tr, kind, cfg: TelegramConfig, run_id=None):
    # Buttons only when the daemon exists to answer them, and only on entries in P1.
    if not cfg.daemon_enabled or kind not in ("long_entry", "short_entry"):
        return None
    try:
        from core.telegram.keyboards import entry_actions
        s, sc = tr.signal, tr.scan
        side = "short" if s.direction == "short" else "long"
        return entry_actions(tr.ticker, float(sc.close), float(s.stop_price),
                             side=side, run_id=run_id)
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
