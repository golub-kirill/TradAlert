"""
Outbound push: send the day's fired signals to Telegram after a scan.

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
import time
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
    # The same regime episode re-fires the caution on every scan (2-3x/day for as
    # long as the regime stays non-BULL). Suppress the repeat — but keyed on what
    # was DELIVERED, not on what fired: the scan journal can't tell a sent
    # caution from a failed push, and suppressing an episode the reader never
    # saw would silence it entirely. Send when no caution was ever delivered,
    # when a BULL run since the last delivery started a new episode, or when a
    # NEW position entered the advisory set; a failed send leaves no delivery
    # record, so the next scan retries automatically.
    cur_caution_set = {tr.ticker.upper() for tr, _k in caution} if caution else set()
    if caution and run_id is not None:
        if _is_repeat_advisory(_caution_state(), cur_caution_set):
            logger.info("[telegram] regime caution suppressed — same episode, "
                        "already delivered, no new positions (%d held)",
                        len(cur_caution_set))
            caution = []
    if not selected and not caution and not cfg.send_stand_down:
        return

    risk_on = _safe_float(getattr(macro_state, "risk_on_score", None))
    n_open = _n_open()
    _first = selected or caution
    regime_label = (_first[0][0].signal.market_regime if _first else _any_regime(results)) or None
    rday = run_date or date.today()

    rejections = (stand_down or {}).get("rejection_gates") or None

    try:
        caution_sent = asyncio.run(
            _send_all(token, chat_id, cfg, selected, len(results),
                      risk_on, n_open, regime_label, rday, rejections, run_id,
                      caution))
        # Journal the DELIVERY only after the send returned — an exception above
        # leaves no record, so the next scan re-sends rather than suppressing an
        # episode the reader never saw (at-least-once, never at-most-zero).
        if caution_sent and caution and run_id is not None:
            _record_caution(run_id, cur_caution_set)
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


def _is_repeat_advisory(state: tuple[set, bool] | None, cur: set) -> bool:
    """True when the last DELIVERED caution already covered every position in ``cur``.

    ``state`` is ``(delivered_set, bull_run_since_delivery)`` from
    ``last_caution_state``, or None when nothing was ever delivered / the DB is
    unreachable / the table doesn't exist yet → False (fail-open: send; the old
    repeat-every-scan behaviour, never silence). A BULL run after the delivery
    means a NEW episode → send even for the same set. A new name in ``cur`` →
    send (the reader needs to know the set grew); names LEAVING need no message.
    """
    if not state or not cur:
        return False
    delivered, bull_since = state
    return not bull_since and bool(delivered) and cur <= delivered


def _caution_state():
    """Last delivered caution + episode-boundary flag; None on any error."""
    try:
        from persistence.db import last_caution_state
        return last_caution_state()
    except Exception as exc:  # noqa: BLE001 — dedup must never break the push
        logger.warning("[telegram] caution-dedup lookup failed — sending: %s", exc)
        return None


def _record_caution(run_id, tickers) -> None:
    """Journal a delivered caution; failures only log (worst case: a re-send)."""
    try:
        from persistence.db import record_caution_sent
        record_caution_sent(run_id, tickers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[telegram] caution delivery not recorded — %s", exc)


def _is_weakening(tr) -> bool:
    """Is this held long's OWN chart deteriorating (vs merely regime-flagged)?

    Weakening = the ticker's trend is no longer UPTREND, or its MACD histogram
    has gone negative on the scan bar. The blanket regime exit flags every held
    long identically; this is the per-position read that separates a broken
    chart from an RSI-70 uptrend that only the INDEX vote turned against.
    Missing fields count as healthy — an incomplete scan row must not shout.
    """
    s = getattr(tr, "signal", None)
    trend = str(getattr(s, "ticker_trend", "") or "")
    if trend and trend != "UPTREND":
        return True
    hist = getattr(getattr(tr, "scan", None), "macd_hist", None)
    try:
        return hist is not None and float(hist) < 0.0
    except (TypeError, ValueError):
        return False


def _caution_message(caution, regime_label):
    """Render the consolidated caution, split by direction (longs vs short covers).

    Longs additionally split into weakening vs still-trending on their own chart.
    """
    longs = [tr.ticker for tr, k in caution if k == "exit_long"]
    shorts = [tr.ticker for tr, k in caution if k == "exit_short"]
    weakening = [tr.ticker for tr, k in caution
                 if k == "exit_long" and _is_weakening(tr)]
    return fmt.format_regime_caution(longs, shorts, regime_label=regime_label,
                                     weakening=weakening)


# ── async send ───────────────────────────────────────────────────────────────────

async def _send_all(token, chat_id, cfg, selected, n_scanned, risk_on, n_open, regime_label, rday,
                    rejections=None, run_id=None, caution=None):
    """Returns True iff the regime caution was actually sent — the caller records
    the delivery only on True, so a failed push is retried next scan rather than
    suppressed as already-seen."""
    from core.telegram.bot import TelegramNotifier

    caution = caution or []

    async with TelegramNotifier(token, chat_id, parse_mode=cfg.parse_mode) as nf:
        if not selected:
            if caution:
                # Nothing else fired — send the regime caution on its own.
                await nf.send_message(_caution_message(caution, regime_label))
                return True
            await nf.send_message(fmt.format_stand_down(
                rday, n_scanned=n_scanned, regime_label=regime_label,
                risk_on=risk_on, n_open=n_open, rejections=rejections))
            return False

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
            await nf.send_message(_caution_message(caution, regime_label))
            return True
        return False


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


# Entry-card panel: split the engine's gate checks into what DECIDED the
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

# A chart is rendered seconds before the push, so anything older belongs to an
# earlier scan. Bounded by AGE rather than by date equality on purpose: the run
# date is UTC while mtime is local, and the two disagree for an evening scan.
_CHART_MAX_AGE_SECONDS = 6 * 3600


def _latest_chart(ticker: str):
    """This scan's screenshot for ``ticker``, or None when the render failed.

    Filenames embed the SIGNAL BAR's date, so a stale file carries a stale chart
    under a current entry/stop caption — worse than no chart. When today's render
    failed (main.py logs "alert still sent") the newest file is an earlier scan's;
    drop it and let the alert go out as text.
    """
    try:
        cands = sorted(SCREENSHOTS_DIR.glob(f"{ticker.upper()}_*.webp"),
                       key=lambda p: p.stat().st_mtime)
        if not cands:
            return None
        newest = cands[-1]
        age = time.time() - newest.stat().st_mtime
        if age > _CHART_MAX_AGE_SECONDS:
            logger.warning(
                "[telegram] %s: newest chart %s is %.1fh old — this scan's render "
                "failed; sending the alert without a chart rather than with a "
                "stale one", ticker, newest.name, age / 3600)
            return None
        return newest
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
