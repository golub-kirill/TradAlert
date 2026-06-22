"""
Interactive Telegram daemon (phase 2) — owner-only command + button handler.

Long-polls Telegram and answers:
  * the phase-1 alert buttons (``open:`` / ``chart:``) and position-card buttons
    (``stop:`` / ``close:`` / ``recalc:`` / ``chartpos:`` / ``confirm:`` / ``cancel``)
  * commands ``/positions /pos /recalc /open /close /stop /status /chart /scan /help``

Every position mutation goes through the broker-adapter seam
(``core.execution.adapter.get_adapter`` → journal only — never auto-executes a
real trade). Reads (charts, recalc, status) bypass the adapter. The send-side
formatters (``core.telegram.format``) and keyboards (``core.telegram.keyboards``)
are shared with the phase-1 push, so the two surfaces never disagree.

Owner-only: every handler is gated on ``TG_CHAT_ID``; a single-instance lockfile
(``data/telegram_bot.lock``) prevents a second poller (Telegram 409 Conflict).

Run:  python telegram_bot.py        (needs telegram.daemon_enabled: true)
Deploy: scripts/register_telegram_bot.ps1 (at-logon Task Scheduler job).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# ── path bootstrap (mirror position_CLI.py): secrets before any os.environ read,
#    then src/ on the path so ``core.*`` imports resolve. ───────────────────────
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "config" / "secrets.env")
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from core import position_manager as pm  # noqa: E402
from core.exits import max_hold_exit_due  # noqa: E402
from core.execution.adapter import get_adapter  # noqa: E402
from core.paths import DATA_DIR, FILTERS_YAML, SCREENSHOTS_DIR, SETTINGS_YAML  # noqa: E402
from exceptions import ValidationError  # noqa: E402
from core.telegram import format as fmt  # noqa: E402
from core.telegram.config import load_telegram_config  # noqa: E402
from core.telegram.keyboards import confirm, position_actions  # noqa: E402

logger = logging.getLogger("telegram_bot")

# Credentials + owner allowlist; set by _load_credentials() at startup. Tests set
# OWNER_ID directly. The owner guard reads OWNER_ID at call time (global lookup).
TOKEN: str | None = None
OWNER_ID: int | None = None

# matplotlib is not thread-safe — serialize chart renders across all handlers.
_CHART_LOCK = asyncio.Lock()

# Held for the process lifetime so the OS releases the lock on exit/crash.
_LOCK_FH = None

# Lazily-built engine (FilterEngine.from_dict) shared by /recalc and /chart.
_ENGINE = None

_REGIME_INDICES = ["SPY", "QQQ"]
_VIX_SYMBOL = "^VIX"


# ── startup: credentials, logging, single-instance lock ──────────────────────

def _load_credentials() -> bool:
    """Read TG_BOT_TOKEN + numeric TG_CHAT_ID into the module globals."""
    global TOKEN, OWNER_ID
    TOKEN = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not TOKEN or not chat:
        return False
    try:
        OWNER_ID = int(chat)
    except (TypeError, ValueError):
        return False
    return True


def _setup_logging() -> None:
    """Root logger → stdout + logs/telegram_bot.log, with the secret-mask filter.

    Installs the shared mask filter BEFORE the bot token is ever used so PTB's
    polling URL / debug lines can't leak it.
    """
    from core.fetchers.http import mask_api_keys_filter

    log_file = DATA_DIR / "telegram_bot.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_h = logging.FileHandler(log_file, encoding="utf-8")
    file_h.setFormatter(formatter)

    mask = mask_api_keys_filter()
    console.addFilter(mask)
    file_h.addFilter(mask)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_h)
    # Tame third-party chatter (httpx logs every getUpdates at INFO).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


def _acquire_lock() -> bool:
    """Take an exclusive lock on data/telegram_bot.lock; False if already held.

    Uses ``msvcrt.locking`` on a held file handle so the OS releases the lock
    automatically when the process exits — even on crash/kill — leaving no stale
    lock to clear by hand (unlike an O_EXCL-created marker file).
    """
    global _LOCK_FH
    try:
        import msvcrt
    except ImportError:
        return True  # non-Windows: best-effort, skip locking

    lock_path = DATA_DIR / "telegram_bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return False
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    _LOCK_FH = fh  # keep referenced for the process lifetime
    return True


# ── shared engine / market context / bar loading ─────────────────────────────

_MAX_OPEN_RISK = None


def _max_open_risk() -> float:
    """The aggregate open-risk budget (``risk.max_open_risk`` in settings; default 5.0)."""
    global _MAX_OPEN_RISK
    if _MAX_OPEN_RISK is None:
        try:
            s = yaml.safe_load(SETTINGS_YAML.read_text(encoding="utf-8")) or {}
            _MAX_OPEN_RISK = float((s.get("risk") or {}).get("max_open_risk", 5.0))
        except Exception:
            _MAX_OPEN_RISK = 5.0
    return _MAX_OPEN_RISK


def _get_engine():
    """Build (once) and return the shared FilterEngine."""
    global _ENGINE
    if _ENGINE is None:
        from core.filter_engine import FilterEngine
        cfg = yaml.safe_load(FILTERS_YAML.read_text(encoding="utf-8"))
        _ENGINE = FilterEngine.from_dict(cfg)
    return _ENGINE


def _load_market_context():
    """Load SPY/QQQ (+ ^VIX) from cache for regime — mirrors main._load_market_context."""
    from persistence.cache import load as cache_load

    market_dfs = {}
    for sym in _REGIME_INDICES:
        try:
            market_dfs[sym] = cache_load(sym)
        except Exception as exc:  # fail-open: regime degrades, never aborts
            logger.debug("[market] %s load failed — %s", sym, exc)
    vix_df = None
    try:
        vix_df = cache_load(_VIX_SYMBOL)
    except Exception as exc:
        logger.debug("[market] %s load failed — %s", _VIX_SYMBOL, exc)
    return (market_dfs or None), vix_df


def _load_bars(ticker: str, fresh: bool = False):
    """attach_indicators on the ticker's bars, or None on failure.

    ``fresh=True`` forces a re-fetch (``get_or_fetch(force=True)``, the same path
    main.py uses) so an on-demand chart regenerated BETWEEN daily scans shows
    current bars rather than the stale cache; it falls back to the cached bars if
    the fetch fails (fail-open) so a chart still renders. Default (cache load) is
    unchanged for position cards.
    """
    try:
        from core.indicators.indicators import attach_indicators
        from persistence.cache import load as cache_load
        if fresh:
            try:
                from persistence.cache import get_or_fetch
                df = get_or_fetch(ticker, force=True)
            except Exception as exc:
                logger.warning("[bars] %s fresh fetch failed (using cache) — %s", ticker, exc)
                df = cache_load(ticker)
        else:
            df = cache_load(ticker)
        return attach_indicators(df)
    except Exception as exc:
        logger.warning("[bars] %s load failed — %s", ticker, exc)
        return None


def _resolve_exit_price(ticker: str) -> float | None:
    """Latest tradeable price for a close: live price (fail-open) → last cached close."""
    try:
        from core.fetchers.live_price import get_live_price
        p = get_live_price(ticker)
        if p:
            return float(p)
    except Exception as exc:
        logger.debug("[price] live %s failed — %s", ticker, exc)
    try:
        from persistence.cache import load as cache_load
        return float(cache_load(ticker)["close"].iloc[-1])
    except Exception as exc:
        logger.debug("[price] cached %s failed — %s", ticker, exc)
    return None


# ── position metrics + engine verdict (read-only) ────────────────────────────

def _basic_metrics(pos, df) -> dict:
    """Unrealized R/%, distance-to-stop, days held, time-stop countdown for a card."""
    import pandas as pd

    last_close = float(df["close"].iloc[-1])
    sign = 1 if pos.side == "long" else -1
    risk = abs(pos.entry_price - pos.stop_price) if pos.stop_price else None
    m: dict = {"now": last_close}
    if pos.entry_price:
        m["unrealized_pct"] = sign * (last_close - pos.entry_price) / pos.entry_price * 100
    if risk:
        m["unrealized_r"] = sign * (last_close - pos.entry_price) / risk
        m["to_stop_r"] = sign * (last_close - pos.stop_price) / risk
    try:
        entry_pos = int(df.index.searchsorted(pd.Timestamp(pos.entry_date)))
        bars_held = max(0, (len(df) - 1) - entry_pos)
        m["days_held"] = bars_held
        exec_cfg = _get_engine().cfg.execution
        mh = exec_cfg.max_hold_days
        if mh is not None:
            m["max_hold"] = int(mh)
            m["mode"] = str(exec_cfg.max_hold_mode).replace("-", "_")
            m["time_stop_left"] = int(mh) - bars_held
    except Exception as exc:
        logger.debug("[metrics] time-stop calc failed for %s — %s", pos.ticker, exc)
    return m


def _engine_verdict(pos, df) -> str:
    """Read-only engine read: would this position exit now? (engine exit or time-stop)."""
    import pandas as pd

    engine = _get_engine()
    market_dfs, vix_df = _load_market_context()
    regime = engine.market_regime(market_dfs, vix_df, empty_vote_trend="CHOP")
    sig = engine.signal(
        pos.ticker, df, market_dfs=market_dfs, vix_df=vix_df,
        held_long=(pos.side == "long"), held_short=(pos.side == "short"),
        regime=regime,
    )
    if sig.passed and sig.direction in ("exit_long", "exit_short"):
        return f"EXIT now — {sig.reason or sig.signal_type}"
    exec_cfg = engine.cfg.execution
    mh = exec_cfg.max_hold_days
    if mh is not None:
        mode = str(exec_cfg.max_hold_mode).replace("-", "_")
        entry_pos = int(df.index.searchsorted(pd.Timestamp(pos.entry_date)))
        bars_held = max(0, (len(df) - 1) - entry_pos)
        if max_hold_exit_due(
                bars_held=bars_held, current_close=float(df["close"].iloc[-1]),
                entry_price=pos.entry_price, side=pos.side,
                max_hold_days=int(mh), mode=mode):
            return f"time-stop due ({int(mh)}d, {mode})"
    return "hold — no exit"


def _build_chart(ticker: str):
    """Render a FRESH chart from the latest bars (distinct from the entry-day snapshot)."""
    from core.indicators.chart import chart

    df = _load_bars(ticker, fresh=True)   # regenerate from a fresh fetch, not stale cache
    if df is None:
        return None
    engine = _get_engine()
    market_dfs, vix_df = _load_market_context()
    regime = engine.market_regime(market_dfs, vix_df, empty_vote_trend="CHOP")
    sig = engine.signal(
        ticker, df, market_dfs=market_dfs, vix_df=vix_df,
        regime=regime, with_checks=True)
    # Populate the data-driven expected-hold range (p25–p75 of real bars_held) so a
    # regenerated chart shows the SAME horizon as the entry alert/caption — not the
    # static SignalResult default.
    if sig.passed and sig.direction in ("long", "short"):
        try:
            from backtest.db import expected_hold_range
            sig.expected_hold_days = expected_hold_range(
                cap=int(engine.cfg.execution.max_hold_days or 25))
        except Exception:
            pass  # keep the SignalResult default
    return chart(ticker, df, signal=(sig if sig.passed else None),
                 output_dir=SCREENSHOTS_DIR, regime=regime)


async def _render_chart(ticker: str):
    """Serialized (matplotlib not thread-safe), off-thread chart render. Path or None."""
    async with _CHART_LOCK:
        try:
            return await asyncio.to_thread(_build_chart, ticker)
        except Exception:
            logger.exception("[chart] render failed for %s", ticker)
            return None


# ── owner guard + callback parsing (pure, unit-testable) ─────────────────────

def _owner_only(handler):
    """Reject any update whose user is not the configured owner.

    Uniform across command AND callback handlers (CallbackQueryHandler has no
    chat-filter registration parameter). A rejected callback is answered so the
    sender's spinner stops; a rejected message is dropped silently.
    """
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or OWNER_ID is None or user.id != OWNER_ID:
            logger.warning("[telegram] rejected non-owner user_id=%s",
                           getattr(user, "id", None))
            if update.callback_query is not None:
                try:
                    await update.callback_query.answer("Not authorized", show_alert=True)
                except Exception:
                    pass
            return
        return await handler(update, context)
    return wrapper


# callback_data 'verb:args…' → exact expected arg count.
_CB_ARITY = {
    "chart": 1, "chartpos": 1, "stop": 1,
    "close": 1, "recalc": 1, "confirm": 2, "cancel": 0,
}
# `open` accepts 3 (legacy, no side → long) or 4 (with explicit side) — so alert
# cards pushed before the side was encoded keep working.
_OPEN_ARITIES = (3, 4)


def parse_callback(data: str | None):
    """Parse compact ``verb:args`` callback_data into ``(verb, args_tuple)``.

    Returns None when the verb is unknown or the arg count is wrong — the router
    answers "unknown action" rather than dispatching a malformed payload.
    """
    if not data:
        return None
    parts = data.split(":")
    verb, args = parts[0], tuple(parts[1:])
    if verb == "open":
        return ("open", args) if len(args) in _OPEN_ARITIES else None
    if verb not in _CB_ARITY or len(args) != _CB_ARITY[verb]:
        return None
    return verb, args


# ── callback handlers (each answers its own query exactly once) ──────────────

async def _cb_open(update, context, args):
    """``open:TICKER:ref:stop[:side]`` — journal the fill (correct direction), disarm the buttons.

    `side` is optional for backward compatibility with cards pushed before it was
    encoded; absent → long (the strategy's default direction).
    """
    query = update.callback_query
    ticker, ref_s, stop_s = args[0], args[1], args[2]
    side = args[3] if len(args) == 4 else "long"
    try:
        ref = float(ref_s)
        stop_val = float(stop_s)
    except ValueError:
        await query.answer("bad price")
        return
    side = side if side in ("long", "short") else "long"
    stop = stop_val if stop_val > 0 else None
    try:
        new_id = await asyncio.to_thread(
            get_adapter().open, ticker.upper(), ref, date.today(), side, stop)
    except ValidationError as exc:
        await query.answer(f"⚠️ {exc.detail}", show_alert=True)
        return
    if new_id:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        notes = []
        if stop is None:
            notes.append("no stop set")
        budget = await asyncio.to_thread(pm.open_risk_advisory, _max_open_risk())
        if budget:
            notes.append(budget)
        suffix = (" · " + " · ".join(notes)) if notes else ""
        await query.answer(f"✅ logged id={new_id}{suffix}", show_alert=bool(notes))
    else:
        await query.answer("⚠️ open failed (see log)")


async def _cb_chart(update, context, args):
    """``chart:TICKER`` — render and send a fresh chart."""
    query = update.callback_query
    await query.answer("📊 rendering…")
    await _send_chart(context, update.effective_chat.id, args[0].upper())


async def _cb_chartpos(update, context, args):
    """``chartpos:ID`` — fresh chart for a position's ticker."""
    query = update.callback_query
    pos = await asyncio.to_thread(pm.get_position, int(args[0]))
    if pos is None:
        await query.answer("position not found")
        return
    await query.answer("📊 rendering…")
    await _send_chart(context, update.effective_chat.id, pos.ticker.upper())


async def _cb_stop(update, context, args):
    """``stop:ID`` — stop changes need a value; point the user at the command."""
    pid = int(args[0])
    await update.callback_query.answer(f"Send  /stop {pid} PRICE", show_alert=True)


async def _cb_close(update, context, args):
    """``close:ID`` — gate the destructive close behind a Yes/No confirm."""
    query = update.callback_query
    pid = int(args[0])
    await query.answer()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"⚠️ Close position #{pid}? Logs an exit at the latest price.",
        reply_markup=confirm("close", str(pid)),
    )


async def _cb_recalc(update, context, args):
    """``recalc:ID`` — reload bars, recompute metrics + engine exit verdict (read-only)."""
    query = update.callback_query
    pid = int(args[0])
    pos = await asyncio.to_thread(pm.get_position, int(pid))
    if pos is None or not pos.is_open:
        await query.answer("no open position #%d" % pid)
        return
    await query.answer("🔄 recomputing…")
    await _send_position_card(context, update.effective_chat.id, pos, with_engine=True)


async def _cb_confirm(update, context, args):
    """``confirm:ACTION:ARG`` — execute the gated action (currently close)."""
    query = update.callback_query
    action, arg = args
    if action != "close":
        await query.answer("unknown confirm")
        return
    pid = int(arg)
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None or not pos.is_open:
        await query.answer("already closed / not found")
        await _safe_edit_text(query, f"#{pid}: already closed or not found")
        return
    price = await asyncio.to_thread(_resolve_exit_price, pos.ticker)
    if price is None:
        await query.answer("⚠️ no price available")
        return
    ok = await asyncio.to_thread(get_adapter().close, pid, price, date.today())
    await query.answer("✅ closed" if ok else "⚠️ close failed")
    await _safe_edit_text(
        query,
        f"{'✅ closed' if ok else '⚠️ failed'} #{pid} {pos.ticker} @ {price:.2f}")


async def _cb_cancel(update, context, args):
    """``cancel`` — dismiss a confirmation."""
    query = update.callback_query
    await query.answer("cancelled")
    await _safe_edit_text(query, "cancelled")


_CB_DISPATCH = {
    "open": _cb_open,
    "chart": _cb_chart,
    "chartpos": _cb_chartpos,
    "stop": _cb_stop,
    "close": _cb_close,
    "recalc": _cb_recalc,
    "confirm": _cb_confirm,
    "cancel": _cb_cancel,
}


@_owner_only
async def _route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-gated callback router: parse callback_data, dispatch, never leave a spinner."""
    query = update.callback_query
    try:
        parsed = parse_callback(query.data)
        if parsed is None:
            await query.answer("unknown action")
            return
        verb, args = parsed
        await _CB_DISPATCH[verb](update, context, args)
    except Exception:
        logger.exception("[telegram] callback error (data=%r)", getattr(query, "data", None))
        try:
            await query.answer("⚠️ error")
        except Exception:
            pass


async def _safe_edit_text(query, text: str) -> None:
    """Edit the message a callback fired on to plain text (drops its buttons); fail-open."""
    try:
        await query.edit_message_text(text)
    except Exception:
        pass


# ── command handlers ─────────────────────────────────────────────────────────

_HELP = (
    "<b>TradAlert bot</b>\n"
    "/positions — open positions (with action buttons)\n"
    "/pos ID — one position card\n"
    "/recalc [ID|all] — recompute P&amp;L + engine exit verdict\n"
    "/open TICKER PRICE [--stop S] [--short] — journal a fill\n"
    "/close ID [PRICE] — close (latest price if omitted)\n"
    "/stop ID PRICE — move the stop\n"
    "/chart TICKER — fresh chart\n"
    "/status — open count + realized R\n"
    "/scan — run the daily scan now"
)


@_owner_only
async def cmd_help(update, context):
    await update.message.reply_text(_HELP)


async def _send_position_card(context, chat_id, pos, *, with_engine: bool) -> None:
    """Render one position card (+ action buttons) and send it."""
    df = await asyncio.to_thread(_load_bars, pos.ticker)
    metrics: dict = {}
    verdict = None
    if df is not None:
        metrics = await asyncio.to_thread(_basic_metrics, pos, df)
        if with_engine:
            try:
                verdict = await asyncio.to_thread(_engine_verdict, pos, df)
            except Exception as exc:
                logger.warning("[recalc] engine verdict failed for %s — %s", pos.ticker, exc)
    text = fmt.format_position_card(pos, engine_verdict=verdict, **metrics)
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=position_actions(pos.id))


async def _send_chart(context, chat_id, ticker: str) -> None:
    path = await _render_chart(ticker)
    if path is None:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ couldn't render {ticker}")
        return
    with Path(path).open("rb") as fh:
        await context.bot.send_photo(
            chat_id=chat_id, photo=fh, caption=f"📈 <b>{ticker}</b> — fresh chart")


@_owner_only
async def cmd_positions(update, context):
    positions = await asyncio.to_thread(pm.load_open_positions)
    if not positions:
        await update.message.reply_text("💼 no open positions")
        return
    for pos in positions.values():
        await _send_position_card(context, update.effective_chat.id, pos, with_engine=False)


@_owner_only
async def cmd_pos(update, context):
    if not context.args:
        await update.message.reply_text("usage: /pos ID")
        return
    try:
        pid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number")
        return
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None:
        await update.message.reply_text(f"no position #{pid}")
        return
    await _send_position_card(context, update.effective_chat.id, pos, with_engine=True)


@_owner_only
async def cmd_recalc(update, context):
    arg = context.args[0].lower() if context.args else "all"
    if arg == "all":
        positions = await asyncio.to_thread(pm.load_open_positions)
        if not positions:
            await update.message.reply_text("💼 no open positions")
            return
        for pos in positions.values():
            await _send_position_card(context, update.effective_chat.id, pos, with_engine=True)
        return
    try:
        pid = int(arg)
    except ValueError:
        await update.message.reply_text("usage: /recalc [ID|all]")
        return
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None or not pos.is_open:
        await update.message.reply_text(f"no open position #{pid}")
        return
    await _send_position_card(context, update.effective_chat.id, pos, with_engine=True)


def _parse_open_args(args):
    """``TICKER PRICE [--stop S] [--short]`` → (ticker, price, side, stop)."""
    if len(args) < 2:
        raise ValueError("usage: /open TICKER PRICE [--stop S] [--short]")
    ticker = args[0].upper()
    price = float(args[1])
    side, stop, rest, i = "long", None, args[2:], 0
    while i < len(rest):
        a = rest[i]
        if a == "--short":
            side, i = "short", i + 1
        elif a == "--stop" and i + 1 < len(rest):
            stop, i = float(rest[i + 1]), i + 2
        else:
            i += 1
    return ticker, price, side, stop


@_owner_only
async def cmd_open(update, context):
    try:
        ticker, price, side, stop = _parse_open_args(context.args)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    try:
        new_id = await asyncio.to_thread(
            get_adapter().open, ticker, price, date.today(), side, stop)
    except ValidationError as exc:
        await update.message.reply_text(f"⚠️ rejected: {exc.detail}")
        return
    if new_id:
        msg = f"✅ opened id={new_id} {side.upper()} {ticker} @ {price:.2f}"
        if stop is None:
            msg += "\n⚠️ no stop set — add one with /stop so it can be scored"
        budget = await asyncio.to_thread(pm.open_risk_advisory, _max_open_risk())
        if budget:
            msg += f"\n⚠️ {budget}"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("⚠️ open failed (see log)")


@_owner_only
async def cmd_close(update, context):
    if not context.args:
        await update.message.reply_text("usage: /close ID [PRICE]")
        return
    try:
        pid = int(context.args[0])
        price = float(context.args[1]) if len(context.args) > 1 else None
    except ValueError:
        await update.message.reply_text("usage: /close ID [PRICE]")
        return
    if price is None:
        pos = await asyncio.to_thread(pm.get_position, pid)
        if pos is None:
            await update.message.reply_text(f"no position #{pid}")
            return
        price = await asyncio.to_thread(_resolve_exit_price, pos.ticker)
        if price is None:
            await update.message.reply_text("⚠️ no price available — pass one: /close ID PRICE")
            return
    ok = await asyncio.to_thread(get_adapter().close, pid, price, date.today())
    await update.message.reply_text(
        f"✅ closed #{pid} @ {price:.2f}" if ok
        else f"⚠️ close failed #{pid} (already closed or not found)")


@_owner_only
async def cmd_stop(update, context):
    try:
        pid = int(context.args[0])
        price = float(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("usage: /stop ID PRICE")
        return
    ok = await asyncio.to_thread(get_adapter().update_stop, pid, price)
    await update.message.reply_text(
        f"✅ stop on #{pid} → {price:.2f}" if ok else f"⚠️ stop update failed #{pid}")


@_owner_only
async def cmd_chart(update, context):
    if not context.args:
        await update.message.reply_text("usage: /chart TICKER")
        return
    await _send_chart(context, update.effective_chat.id, context.args[0].upper())


def _realized_summary() -> str | None:
    """One-line realized-R summary across closed positions (lazy reconcile, fail-open)."""
    try:
        from scripts.reconcile_fills import reconcile
        closed = [p for p in pm.list_all() if not p.is_open]
        if not closed:
            return None
        res = reconcile(closed)
        rs = [r for side in res["by_side"].values() for r in side]
        if not rs:
            return None
        total = sum(rs)
        return f"📈 realized {total:+.2f}R over {len(rs)} closed (avg {total / len(rs):+.3f}R)"
    except Exception as exc:
        logger.debug("[status] realized summary skipped — %s", exc)
        return None


@_owner_only
async def cmd_status(update, context):
    open_pos = await asyncio.to_thread(pm.load_open_positions)
    lines = [f"💼 <b>{len(open_pos)}</b> open position(s)"]
    if open_pos:
        lines.append(" · ".join(sorted(open_pos.keys())))
    summary = await asyncio.to_thread(_realized_summary)
    if summary:
        lines.append(summary)
    await update.message.reply_text("\n".join(lines))


@_owner_only
async def cmd_scan(update, context):
    """Run the daily scan as a subprocess; it does its own phase-1 push."""
    msg = await update.message.reply_text("🔄 scan started…")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "main.py", cwd=str(ROOT))
        rc = await proc.wait()
    except Exception as exc:
        logger.exception("[scan] subprocess failed")
        await msg.edit_text(f"⚠️ scan failed to launch — {exc}")
        return
    await msg.edit_text(
        f"{'✅' if rc == 0 else '⚠️'} scan finished (rc={rc}). "
        "Any alerts were pushed by the run.")


# ── catch-all + error handler ────────────────────────────────────────────────

async def _reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Non-command messages: hint the owner, drop strangers."""
    user = update.effective_user
    if user is not None and OWNER_ID is not None and user.id == OWNER_ID:
        if update.message is not None:
            await update.message.reply_text("Unknown input — try /help")
    else:
        logger.warning("[telegram] ignoring message from non-owner user_id=%s",
                       getattr(user, "id", None))


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("[telegram] handler error: %s", context.error, exc_info=context.error)


# ── application wiring (factored out so tests can build without polling) ──────

def build_application(token: str) -> Application:
    """Build a PTB Application with all handlers registered (no network yet)."""
    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pos", cmd_pos))
    app.add_handler(CommandHandler("recalc", cmd_recalc))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(_route))
    app.add_handler(MessageHandler(filters.ALL, _reject))
    app.add_error_handler(_on_error)
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="telegram_bot", description="TradAlert interactive Telegram daemon.")
    parser.parse_args(argv)

    _setup_logging()

    if not _load_credentials():
        logger.error("TG_BOT_TOKEN / numeric TG_CHAT_ID missing in config/secrets.env — exiting")
        return 1

    cfg = load_telegram_config(yaml.safe_load(SETTINGS_YAML.read_text(encoding="utf-8")))
    if not cfg.daemon_enabled:
        logger.warning("telegram.daemon_enabled is false — nothing to do, exiting")
        return 0

    if not _acquire_lock():
        logger.error("another telegram_bot instance is already polling — exiting")
        return 0

    app = build_application(TOKEN)
    logger.info("telegram_bot polling (owner=%s)…", OWNER_ID)
    # stop_signals=None: don't install SIGINT/SIGTERM handlers (Windows / non-main
    # thread). drop_pending_updates: a restart must not replay a stale destructive tap.
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        stop_signals=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
