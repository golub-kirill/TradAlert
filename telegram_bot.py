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
Deploy: scripts/setup/register_telegram_bot.ps1 (at-logon Task Scheduler job).
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
from telegram import ForceReply, Update  # noqa: E402
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
from core.telegram.keyboards import (  # noqa: E402
    confirm, position_actions, positions_table_actions, status_actions,
)

logger = logging.getLogger("telegram_bot")

# Credentials + owner allowlist; set by _load_credentials() at startup. Tests set
# OWNER_ID directly. The owner guard reads OWNER_ID at call time (global lookup).
TOKEN: str | None = None
OWNER_ID: int | None = None

# matplotlib is not thread-safe — serialize chart renders across all handlers.
_CHART_LOCK = asyncio.Lock()

# Pending custom-fill prompts: owner chat_id → (prompt_msg_id, ticker, stop, side).
# Set when "✍️ Custom" is tapped; consumed (one-shot) only by a REPLY to that exact
# ForceReply prompt (prompt_msg_id), so it can't cross-talk with a pending edit.
_PENDING_FILL: dict[int, tuple[int | None, str, float, str]] = {}

# Pending field edits: owner chat_id → (prompt_msg_id, position_id, column). Same
# reply-bound, one-shot consumption as _PENDING_FILL.
_PENDING_EDIT: dict[int, tuple[int | None, int, str]] = {}

# Edit field alias → positions column. The bot/CLI exposes friendly names; the
# data layer (update_position) whitelists the actual columns.
_EDIT_FIELDS = {
    "entry": "entry_price", "stop": "stop_price", "exit": "exit_price",
    "initial": "initial_stop", "notes": "notes",
}

# Held for the process lifetime so the OS releases the lock on exit/crash.
_LOCK_FH = None

# Lazily-built engine (FilterEngine.from_dict) shared by /recalc and /chart.
_ENGINE = None

_REGIME_INDICES = ["SPY", "QQQ"]  # fallback; _regime_indices() reads the config knob
_VIX_SYMBOL = "^VIX"


def _regime_indices() -> list[str]:
    """Regime index symbols from ``filters.regime.index_symbols`` (fallback SPY/QQQ)."""
    try:
        cfg = yaml.safe_load(FILTERS_YAML.read_text(encoding="utf-8")) or {}
        idx = (cfg.get("regime") or {}).get("index_symbols")
        return [str(s) for s in idx] if idx else list(_REGIME_INDICES)
    except Exception:
        return list(_REGIME_INDICES)


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
    for sym in _regime_indices():
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
                from core.fetchers.yf_fetchOne import fetch as _fetch_one
                df = get_or_fetch(ticker, _fetch_one, force=True)
            except Exception as exc:
                logger.warning("[bars] %s fresh fetch failed (using cache) — %s", ticker, exc)
                df = cache_load(ticker)
        else:
            df = cache_load(ticker)
        return attach_indicators(df)
    except Exception as exc:
        logger.warning("[bars] %s load failed — %s", ticker, exc)
        return None


def _live_price(ticker: str) -> float | None:
    """Live quote ONLY (no cache fallback) — used for an honest fill price.

    Unlike ``_resolve_exit_price`` this never falls back to the stale cached
    close: a fill logged "@ live" must be a real live quote or fail, so the user
    is told to use @ ref / custom rather than silently journaling a stale price.
    """
    try:
        from core.fetchers.live_price import get_live_price
        p = get_live_price(ticker)
        return float(p) if p else None
    except Exception as exc:
        logger.debug("[fill] live price %s failed — %s", ticker, exc)
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

def _basic_metrics(pos, df, now_price: float | None = None) -> dict:
    """Unrealized R/%, distance-to-stop, days held, time-stop countdown for a card.

    ``now_price`` (the LIVE price) drives "now" and the live PnL/distance figures;
    it falls back to the last daily close when no live price is available. The bar
    df is still used for days-held and the time-stop countdown.
    """
    import pandas as pd

    last_close = float(df["close"].iloc[-1])
    cur = float(now_price) if now_price is not None else last_close
    sign = 1 if pos.side == "long" else -1
    risk = abs(pos.entry_price - pos.stop_price) if pos.stop_price else None
    m: dict = {"now": cur}
    if pos.entry_price:
        m["unrealized_pct"] = sign * (cur - pos.entry_price) / pos.entry_price * 100
    if risk:
        m["unrealized_r"] = sign * (cur - pos.entry_price) / risk
        m["to_stop_r"] = sign * (cur - pos.stop_price) / risk
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


def _closed_metrics(pos) -> dict:
    """Realized R/% + days held for a CLOSED position card (no live price needed)."""
    m: dict = {}
    if pos.exit_price is None or not pos.entry_price:
        return m
    sign = 1 if pos.side == "long" else -1
    m["unrealized_pct"] = sign * (pos.exit_price - pos.entry_price) / pos.entry_price * 100
    risk_stop = pos.initial_stop if pos.initial_stop is not None else pos.stop_price
    if risk_stop:
        risk = abs(pos.entry_price - risk_stop)
        if risk > 0:
            m["unrealized_r"] = sign * (pos.exit_price - pos.entry_price) / risk
    if pos.entry_date and pos.exit_date:
        m["days_held"] = (pos.exit_date - pos.entry_date).days
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
    # A fired entry → its real trigger panel + SL/TP. Otherwise (the common case for an
    # on-demand /chart) build a display scoreboard so the chart still shows the full
    # indicator factor panel, not just the bare "Current Values" box.
    chart_signal = sig if (sig.passed and sig.checks) else engine.scoreboard(
        ticker, df, regime=regime, market_dfs=market_dfs)
    return chart(ticker, df, signal=chart_signal,
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
            return None
        return await handler(update, context)
    return wrapper


# callback_data 'verb:args…' → exact expected arg count.
_CB_ARITY = {
    "chart": 1, "chartpos": 1, "stop": 1, "stopbe": 1, "stop1r": 1,
    "close": 1, "closemenu": 1, "partial": 2, "recalc": 1, "confirm": 2, "cancel": 0,
    "logmenu": 4, "fill": 5, "skip": 2, "editmenu": 1, "edit": 2, "status": 1,
    "posrefresh": 1, "poscards": 1,
}

# ½ / ⅓ scale-out fractions for the partial-close buttons.
_PARTIAL_FRACTIONS = {"half": 0.5, "third": round(1.0 / 3.0, 4)}
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


# ── pure stop-level helpers (unit-testable; no DB / no network) ──────────────

def _one_r_stop(pos) -> float | None:
    """The +1R stop level (locks in 1R of profit) from the INITIAL risk unit.

    long:  entry + (entry - initial_stop);  short: entry - (initial_stop - entry).
    Uses the frozen ``initial_stop`` (the reconciliation risk unit), falling back
    to the current ``stop_price`` for legacy rows. None when there is no usable
    stop or the geometry is degenerate (no risk unit to project)."""
    risk_stop = pos.initial_stop if pos.initial_stop is not None else pos.stop_price
    if risk_stop is None:
        return None
    r = pm.risk_unit(pos.side, float(pos.entry_price), float(risk_stop))
    if r <= 0:
        return None
    entry = float(pos.entry_price)
    level = entry + r if pos.side == "long" else entry - r
    return round(level, 4)


def _stop_market_note(side: str, stop: float, current: float | None) -> str:
    """' ⚠ …' when the new stop sits on the wrong side of the latest price (so it
    would stop out immediately), else ''. Empty when no price is available."""
    if current is None:
        return ""
    breached = (current <= stop) if side == "long" else (current >= stop)
    return " ⚠ price already past it — would stop out" if breached else ""


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


async def _do_fill(ticker: str, price: float, stop: float | None, side: str):
    """Journal a fill via the adapter. Returns ``(new_id|None, status_message)`` —
    the shared body for the live/ref buttons and the custom force-reply."""
    stop_val = stop if (stop and stop > 0) else None
    try:
        new_id = await asyncio.to_thread(
            get_adapter().open, ticker.upper(), price, date.today(), side, stop_val)
    except ValidationError as exc:
        return None, f"⚠️ {exc.detail}"
    if not new_id:
        return None, "⚠️ open failed (see log)"
    notes = []
    if stop_val is None:
        notes.append("no stop set")
    budget = await asyncio.to_thread(pm.open_risk_advisory, _max_open_risk())
    if budget:
        notes.append(budget)
    suffix = (" · " + " · ".join(notes)) if notes else ""
    return new_id, f"✅ logged id={new_id} {side.upper()} {ticker.upper()} @ {price:.2f}{suffix}"


async def _cb_logmenu(update, context, args):
    """``logmenu:TICKER:ref:stop:side`` — open the fill-price picker on the card."""
    from core.telegram.keyboards import fill_source_menu
    query = update.callback_query
    ticker, ref_s, stop_s, side = args
    try:
        ref, stop = float(ref_s), float(stop_s)
    except ValueError:
        await query.answer("bad price")
        return
    side = side if side in ("long", "short") else "long"
    await query.answer()
    menu = fill_source_menu(ticker, ref, stop, side)
    try:
        await query.edit_message_reply_markup(reply_markup=menu)
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"How did you fill {ticker.upper()}?", reply_markup=menu)


async def _cb_fill(update, context, args):
    """``fill:SRC:TICKER:ref:stop:side`` — journal the fill.

    SRC: ``live`` (a real live quote — never the stale cache), ``ref`` (the alert
    price), or ``cust`` (prompt for a typed price via force-reply).
    """
    query = update.callback_query
    src, ticker, ref_s, stop_s, side = args
    side = side if side in ("long", "short") else "long"
    try:
        ref, stop = float(ref_s), float(stop_s)
    except ValueError:
        await query.answer("bad price")
        return

    if src == "cust":
        await query.answer()
        prompt = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✍️ Reply with the fill price for {ticker.upper()} ({side}).",
            reply_markup=ForceReply(selective=True))
        # Bind the pending fill to THIS prompt's message id so a reply only resolves
        # the prompt it actually answered (no cross-talk with a pending edit).
        _PENDING_FILL[update.effective_chat.id] = (_prompt_id(prompt), ticker, stop, side)
        return

    if src == "live":
        price = await asyncio.to_thread(_live_price, ticker)
        if price is None:
            await query.answer("⚠️ no live quote — use @ ref or ✍️ Custom", show_alert=True)
            return
    else:  # ref
        price = ref

    new_id, msg = await _do_fill(ticker, price, stop, side)
    if new_id:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    await query.answer(msg, show_alert=msg.startswith("⚠️"))


def _prompt_id(msg) -> int | None:
    """Message id of a sent ForceReply prompt (None if unavailable)."""
    return getattr(msg, "message_id", None)


def _reply_matches(update, prompt_id) -> bool:
    """True iff this message is a reply to the specific ForceReply prompt
    ``prompt_id`` — so a typed answer only resolves the prompt it actually
    answered (no cross-talk between a pending fill and a pending edit, and no
    hijack of an unrelated free-text message)."""
    if prompt_id is None:
        return False
    reply = getattr(update.message, "reply_to_message", None)
    return reply is not None and getattr(reply, "message_id", None) == prompt_id


async def _try_custom_fill(update, context) -> bool:
    """One-shot: if THIS message replies to a pending custom-fill prompt, journal
    the typed price and return True (consumed); else return False (not for us)."""
    pending = _PENDING_FILL.get(update.effective_chat.id)
    if pending is None:
        return False
    prompt_id, ticker, stop, side = pending
    if not _reply_matches(update, prompt_id):
        return False
    _PENDING_FILL.pop(update.effective_chat.id, None)
    text = (update.message.text or "").strip()
    try:
        price = float(text.split()[0])
    except (ValueError, IndexError):
        await update.message.reply_text(
            f"⚠️ couldn't read a price for {ticker.upper()} — fill not logged")
        return True
    _new_id, msg = await _do_fill(ticker, price, stop, side)
    await update.message.reply_text(msg)
    return True


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


async def _cb_stopbe(update, context, args):
    """``stopbe:ID`` — one tap: move the stop to breakeven (the entry price)."""
    await _move_stop(update, args, target=lambda pos: round(float(pos.entry_price), 4),
                     label="breakeven")


async def _cb_stop1r(update, context, args):
    """``stop1r:ID`` — one tap: move the stop to the +1R level (locks in 1R)."""
    await _move_stop(update, args, target=_one_r_stop, label="+1R",
                     none_msg="no initial stop on #%d — can't compute +1R")


async def _move_stop(update, args, *, target, label, none_msg=None):
    """Shared body for the one-tap stop moves: resolve the position, compute the
    new stop via ``target(pos)``, journal it with ``update_stop``, and report —
    warning if the new stop already sits past the live price."""
    query = update.callback_query
    pid = int(args[0])
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None or not pos.is_open:
        await query.answer("no open position #%d" % pid)
        return
    new_stop = target(pos)
    if new_stop is None:
        await query.answer("⚠️ " + ((none_msg or "can't move stop on #%d") % pid),
                           show_alert=True)
        return
    ok = await asyncio.to_thread(get_adapter().update_stop, pid, new_stop)
    if not ok:
        await query.answer("⚠️ stop update failed")
        return
    current = await asyncio.to_thread(_resolve_exit_price, pos.ticker)
    note = _stop_market_note(pos.side, new_stop, current)
    await query.answer(f"✅ stop → {label} {new_stop:.2f}{note}", show_alert=bool(note))


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


async def _cb_closemenu(update, context, args):
    """``closemenu:ID`` — show the close/scale picker (½ / ⅓ / Full)."""
    from core.telegram.keyboards import close_menu
    query = update.callback_query
    pid = int(args[0])
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None or not pos.is_open:
        await query.answer("no open position #%d" % pid)
        return
    await query.answer()
    menu = close_menu(pid)
    try:
        await query.edit_message_reply_markup(reply_markup=menu)
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Close #{pid} {pos.ticker}?", reply_markup=menu)


async def _cb_partial(update, context, args):
    """``partial:SRC:ID`` — journal a ½/⅓ scale-out at the latest price (manual
    risk tool). The remaining fraction closes later via the Full close; reconcile
    weights realized R by the fractions."""
    query = update.callback_query
    src, pid_s = args
    pid = int(pid_s)
    frac = _PARTIAL_FRACTIONS.get(src)
    if frac is None:
        await query.answer("unknown size")
        return
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None or not pos.is_open:
        await query.answer("no open position #%d" % pid)
        return
    price = await asyncio.to_thread(_resolve_exit_price, pos.ticker)
    if price is None:
        await query.answer("⚠️ no price available", show_alert=True)
        return
    try:
        new_id = await asyncio.to_thread(
            get_adapter().scale_out, pid, price, date.today(), frac)
    except ValidationError as exc:
        await query.answer(f"⚠️ {exc.detail}", show_alert=True)
        return
    if not new_id:
        await query.answer("⚠️ scale-out failed (see log)")
        return
    remaining = await asyncio.to_thread(pm.remaining_fraction, pid)
    await query.answer(
        f"✅ scaled {frac:.0%} of {pos.ticker} @ {price:.2f} · {remaining:.0%} left",
        show_alert=True)


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


async def _cb_skip(update, context, args):
    """``skip:RUNID:TICKER`` — journal the owner skipping a fired entry (declined),
    feeding opportunity_tracker's passed-on outcomes (was skipping it right?)."""
    from persistence.db import mark_declined
    query = update.callback_query
    run_id_s, ticker = args
    try:
        run_id = int(run_id_s)
    except ValueError:
        await query.answer("bad run id")
        return
    ok = await asyncio.to_thread(mark_declined, run_id, ticker)
    if ok:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer(f"🚫 skipped {ticker.upper()} — logged as passed-on")
    else:
        await query.answer("⚠️ couldn't log skip (see log)")


# ── edit a journaled position (open or closed) ───────────────────────────────

async def _apply_edit(pid: int, col: str, raw: str):
    """Validate + apply one field edit via the adapter. Returns ``(ok, message)`` —
    shared by the /edit command and the ✏️ Edit force-reply."""
    if col == "notes":
        value = raw
    else:
        try:
            value = float(raw)
        except ValueError:
            return False, f"⚠️ '{raw}' is not a number"
    try:
        ok = await asyncio.to_thread(
            lambda: get_adapter().edit_position(pid, **{col: value}))
    except ValidationError as exc:
        return False, f"⚠️ {exc.detail}"
    if ok:
        shown = value if col == "notes" else f"{float(value):.4f}"
        return True, f"✅ #{pid} {col} → {shown}"
    return False, f"⚠️ edit failed for #{pid} (not found?)"


async def _cb_editmenu(update, context, args):
    """``editmenu:ID`` — show the field picker (open vs closed offer different fields)."""
    from core.telegram.keyboards import edit_menu
    query = update.callback_query
    pid = int(args[0])
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None:
        await query.answer("no position #%d" % pid)
        return
    await query.answer()
    menu = edit_menu(pid, pos.is_open)
    try:
        await query.edit_message_reply_markup(reply_markup=menu)
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Edit #{pid} {pos.ticker}?", reply_markup=menu)


async def _cb_edit(update, context, args):
    """``edit:FIELD:ID`` — prompt (force-reply) for the new value of one field."""
    query = update.callback_query
    field, pid_s = args
    col = _EDIT_FIELDS.get(field)
    if col is None:
        await query.answer("unknown field")
        return
    pid = int(pid_s)
    pos = await asyncio.to_thread(pm.get_position, pid)
    if pos is None:
        await query.answer("no position #%d" % pid)
        return
    await query.answer()
    prompt = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✏️ Reply with the new <b>{field}</b> for #{pid} {pos.ticker}.",
        reply_markup=ForceReply(selective=True))
    # Bind the pending edit to THIS prompt's message id (see _reply_matches).
    _PENDING_EDIT[update.effective_chat.id] = (_prompt_id(prompt), pid, col)


async def _try_pending_edit(update, context) -> bool:
    """One-shot: if THIS message replies to a pending field-edit prompt, apply the
    typed value and return True (consumed); else return False (not for us)."""
    pending = _PENDING_EDIT.get(update.effective_chat.id)
    if pending is None:
        return False
    prompt_id, pid, col = pending
    if not _reply_matches(update, prompt_id):
        return False
    _PENDING_EDIT.pop(update.effective_chat.id, None)
    raw = (update.message.text or "").strip()
    _ok, msg = await _apply_edit(pid, col, raw)
    await update.message.reply_text(msg)
    return True


async def _cb_status(update, context, args):
    """Refresh the /status dashboard in place (the 🔄 Refresh button)."""
    query = update.callback_query
    text = await asyncio.to_thread(_render_status)
    try:
        await query.edit_message_text(text, reply_markup=status_actions())
    except Exception:
        pass  # "message is not modified" when nothing changed since last render
    await query.answer("refreshed")


async def _cb_posrefresh(update, context, args):
    """Re-render the compact /positions table in place (🔄 Refresh)."""
    query = update.callback_query
    text = await asyncio.to_thread(_render_positions_table)
    try:
        await query.edit_message_text(text, reply_markup=positions_table_actions())
    except Exception:
        pass  # "message is not modified" when nothing moved since last render
    await query.answer("refreshed")


async def _cb_poscards(update, context, args):
    """Switch from the compact table to the per-position action cards (🃏 Cards)."""
    query = update.callback_query
    await query.answer("cards")
    positions = await asyncio.to_thread(pm.load_open_positions)
    chat_id = update.effective_chat.id
    if not positions:
        await context.bot.send_message(chat_id=chat_id, text="💼 no open positions")
        return
    for pos in positions.values():
        await _send_position_card(context, chat_id, pos, with_engine=False)


_CB_DISPATCH = {
    "open": _cb_open,
    "logmenu": _cb_logmenu,
    "fill": _cb_fill,
    "chart": _cb_chart,
    "chartpos": _cb_chartpos,
    "stop": _cb_stop,
    "stopbe": _cb_stopbe,
    "stop1r": _cb_stop1r,
    "close": _cb_close,
    "closemenu": _cb_closemenu,
    "partial": _cb_partial,
    "recalc": _cb_recalc,
    "confirm": _cb_confirm,
    "cancel": _cb_cancel,
    "skip": _cb_skip,
    "editmenu": _cb_editmenu,
    "edit": _cb_edit,
    "status": _cb_status,
    "posrefresh": _cb_posrefresh,
    "poscards": _cb_poscards,
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
    "📟 <b>TradAlert</b> — interactive controls\n"
    "<blockquote>journal-only · every button drives the positions table, never a broker</blockquote>\n"
    "\n"
    "<b>📊 View</b>\n"
    "<code>/positions</code> — compact table of all open positions "
    "(<code>cards</code> for per-position action cards)\n"
    "<code>/pos ID</code> — one card (open <i>or</i> closed)\n"
    "<code>/status</code> — open count + realized R\n"
    "<code>/chart TICKER</code> — fresh chart + factor scoreboard\n"
    "\n"
    "<b>📈 Journal</b>\n"
    "<code>/open TICKER PRICE [--stop S] [--short]</code> — log a fill\n"
    "<code>/close ID [PRICE]</code> — close (live price if omitted)\n"
    "<code>/stop ID PRICE</code> — move the stop\n"
    "<code>/edit ID FIELD VALUE</code> — fix a fill · fields: "
    "<i>entry · stop · exit · initial · notes</i>\n"
    "<code>/recalc [ID|all]</code> — recompute PnL + engine exit read\n"
    "\n"
    "<b>🔔 Alerts</b>\n"
    "<code>/alert TICKER above|below PRICE</code> — arm a price alert\n"
    "<code>/alerts</code> — list active · <code>/alert del ID</code> to remove\n"
    "\n"
    "<b>⚙️ Run</b>\n"
    "<code>/scan</code> — run the daily scan now\n"
    "\n"
    "<b>🔘 Buttons</b>\n"
    "<i>Entry</i> → 📈 Log opened (live · ref · ✍️ custom) · 🚫 Skip · 📊 Chart\n"
    "<i>Position</i> → 🟰 Breakeven · 🔒 +1R · ✏️ Stop · ➖ Close (½·⅓·Full) · "
    "✏️ Edit · 🔄 Recalc"
)


@_owner_only
async def cmd_help(update, context):
    await update.message.reply_text(_HELP)


async def _send_position_card(context, chat_id, pos, *, with_engine: bool) -> None:
    """Render one position card (+ action buttons) and send it.

    A CLOSED position gets a realized-R card with only the ✏️ Edit / 📈 Chart
    actions (the live stop/close/recalc buttons need an open position)."""
    if not pos.is_open:
        text = fmt.format_position_card(pos, closed=True, **_closed_metrics(pos))
        await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=position_actions(pos.id, is_open=False))
        return
    df = await asyncio.to_thread(_load_bars, pos.ticker)
    # "now" / live PnL use the LIVE price (fail-open to the last close), not the
    # cached daily close — a position opened at today's close otherwise reads as
    # flat all day while the tape moves.
    live = await asyncio.to_thread(_resolve_exit_price, pos.ticker)
    metrics: dict = {}
    verdict = None
    if df is not None:
        metrics = await asyncio.to_thread(_basic_metrics, pos, df, live)
        if with_engine:
            try:
                verdict = await asyncio.to_thread(_engine_verdict, pos, df)
            except Exception as exc:
                logger.warning("[recalc] engine verdict failed for %s — %s", pos.ticker, exc)
    # Surface any manual scale-outs (remaining < 100%); fail-open to no line.
    partials = await asyncio.to_thread(pm.get_partials, pos.id)
    remaining = round(1.0 - sum(p.fraction for p in partials), 6) if partials else None
    text = fmt.format_position_card(pos, engine_verdict=verdict,
                                    remaining_frac=remaining, **metrics)
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


def _render_positions_table() -> str:
    """Build the compact /positions table (sync; fail-open per position).

    One line per open position with live PnL / R / distance-to-stop, plus the
    open-risk budget + realized-R footer. A ticker whose bars/quote can't be
    fetched still lists (identity line only) — one bad symbol never drops the table.
    """
    try:
        open_pos = pm.load_open_positions()
    except Exception as exc:
        logger.debug("[positions] load failed — %s", exc)
        open_pos = {}
    rows: list[dict] = []
    for pos in open_pos.values():
        row = {"ticker": pos.ticker, "id": pos.id, "side": pos.side}
        try:
            df = _load_bars(pos.ticker)
            if df is not None:
                m = _basic_metrics(pos, df, _resolve_exit_price(pos.ticker))
                for k in ("unrealized_r", "unrealized_pct", "to_stop_r", "days_held"):
                    if k in m:
                        row[k] = m[k]
        except Exception as exc:
            logger.debug("[positions] metrics failed for %s — %s", pos.ticker, exc)
        rows.append(row)
    budget = realized = None
    try:
        budget = pm.open_risk_advisory(_max_open_risk())
    except Exception as exc:
        logger.debug("[positions] budget note skipped — %s", exc)
    try:
        realized = _realized_summary()
    except Exception as exc:
        logger.debug("[positions] realized note skipped — %s", exc)
    return fmt.format_positions_table(rows, budget_note=budget, realized_note=realized)


@_owner_only
async def cmd_positions(update, context):
    """Default: a compact one-message table of all open positions. ``/positions cards``
    sends the per-position action cards (the original behavior)."""
    if context.args and str(context.args[0]).lower().startswith("card"):
        positions = await asyncio.to_thread(pm.load_open_positions)
        if not positions:
            await update.message.reply_text("💼 no open positions")
            return
        for pos in positions.values():
            await _send_position_card(context, update.effective_chat.id, pos, with_engine=False)
        return
    text = await asyncio.to_thread(_render_positions_table)
    await update.message.reply_text(text, reply_markup=positions_table_actions())


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
async def cmd_edit(update, context):
    """``/edit ID FIELD VALUE`` — correct a journaled position (open or closed)."""
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "usage: /edit ID FIELD VALUE\nfields: entry · stop · exit · initial · notes")
        return
    try:
        pid = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number")
        return
    col = _EDIT_FIELDS.get(args[1].lower())
    if col is None:
        await update.message.reply_text(
            f"unknown field '{args[1]}' — use: entry · stop · exit · initial · notes")
        return
    _ok, msg = await _apply_edit(pid, col, " ".join(args[2:]))
    await update.message.reply_text(msg)


@_owner_only
async def cmd_chart(update, context):
    if not context.args:
        await update.message.reply_text("usage: /chart TICKER")
        return
    await _send_chart(context, update.effective_chat.id, context.args[0].upper())


def _realized_summary() -> str | None:
    """One-line realized-R summary across closed positions (lazy reconcile, fail-open)."""
    try:
        from scripts.live.reconcile_fills import reconcile
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


def _render_status() -> str:
    """Build the /status dashboard text (sync; all blocking DB/IO, fail-open per panel):
    open positions + open-risk-vs-budget, realized R, and the latest scan's
    fired/scanned counts + top stand-down blocks.

    DB-sourced strings (budget, tickers, gate reasons, regime) are HTML-ESCAPED:
    the daemon sends with parse_mode=HTML, so a reason like ``rr < min_rr`` would
    otherwise be read as a stray tag and Telegram rejects the WHOLE reply
    (BadRequest: unsupported start tag) — the structural <b>…</b> stay literal."""
    import html

    def esc(x) -> str:
        return html.escape(str(x), quote=False)

    lines = ["📊 <b>Status</b>"]
    try:
        open_pos = pm.load_open_positions()
        head = f"💼 <b>{len(open_pos)}</b> open position(s)"
        budget = pm.open_risk_advisory(_max_open_risk())
        if budget:
            head += f" · {esc(budget)}"
        lines.append(head)
        if open_pos:
            lines.append(esc(" · ".join(sorted(open_pos.keys()))))
    except Exception as exc:
        logger.debug("[status] open-positions panel skipped — %s", exc)

    summary = _realized_summary()
    if summary:
        lines.append(esc(summary))

    try:
        from persistence.db import latest_scan_run, stand_down_summary
        run = latest_scan_run()
        if run:
            scan_line = (f"🔎 last scan: {int(run.get('signals_fired') or 0)} fired · "
                         f"{int(run.get('tickers_scanned') or 0)} scanned · "
                         f"{esc(run.get('market_regime') or '—')}")
            sd = stand_down_summary(run["run_id"])
            gates = (sd or {}).get("rejection_gates") or []
            if gates:
                top = " · ".join(f"{esc(g['gate'])} ×{int(g['n'])}" for g in gates[:3])
                scan_line += f"\n   🚧 top blocks: {top}"
            lines.append(scan_line)
    except Exception as exc:
        logger.debug("[status] scan panel skipped — %s", exc)

    return "\n".join(lines)


@_owner_only
async def cmd_status(update, context):
    text = await asyncio.to_thread(_render_status)
    await update.message.reply_text(text, reply_markup=status_actions())


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

async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Non-command messages from the owner: a pending custom-fill price (force-reply
    answer) is journaled; anything else gets the /help hint. Strangers are dropped."""
    user = update.effective_user
    if user is None or OWNER_ID is None or user.id != OWNER_ID:
        logger.warning("[telegram] ignoring message from non-owner user_id=%s",
                       getattr(user, "id", None))
        return
    if update.message is None:
        return
    if await _try_custom_fill(update, context):
        return
    if await _try_pending_edit(update, context):
        return
    await update.message.reply_text("Unknown input — try /help")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("[telegram] handler error: %s", context.error, exc_info=context.error)


# ── application wiring (factored out so tests can build without polling) ──────

# ── price alerts (owner-set target crossings; alerting-only, never traded) ────

_ALERT_POLL_INTERVAL_S = 300  # 5 minutes


def _alert_crossed(direction: str, target: float, price: float) -> bool:
    """True once `price` reaches/passes the alert `target` in `direction`."""
    if direction == "above":
        return price >= target
    if direction == "below":
        return price <= target
    return False


def _us_market_open_now() -> bool:
    """Rough RTH gate: NYSE weekday 09:30–16:00 ET (holidays ignored — a check on a
    closed day just finds an unchanged price; fail-open to True if tz lookup fails)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


@_owner_only
async def cmd_alert(update, context):
    """Arm or delete a price alert.

    ``/alert TICKER above|below PRICE`` arms one; ``/alert del ID`` removes one."""
    from persistence.db import add_price_alert, deactivate_price_alert
    args = [str(a) for a in (context.args or [])]
    if len(args) == 2 and args[0].lower() == "del" and args[1].isdigit():
        ok = await asyncio.to_thread(deactivate_price_alert, int(args[1]))
        await update.message.reply_text("🗑 alert removed" if ok else "no such active alert")
        return
    if len(args) != 3 or args[1].lower() not in ("above", "below"):
        await update.message.reply_text(
            "usage: /alert TICKER above|below PRICE   ·   /alert del ID")
        return
    ticker, direction = args[0].upper(), args[1].lower()
    try:
        price = float(args[2])
    except ValueError:
        await update.message.reply_text("bad price")
        return
    aid = await asyncio.to_thread(add_price_alert, ticker, direction, price)
    if aid is None:
        await update.message.reply_text("⚠️ couldn't save alert (see log)")
        return
    arrow = "▲" if direction == "above" else "▼"
    await update.message.reply_text(
        f"🔔 alert #{aid}: {fmt._esc(ticker)} {arrow} {direction} {price:.2f}")


@_owner_only
async def cmd_alerts(update, context):
    """List active price alerts."""
    from persistence.db import list_price_alerts
    alerts = await asyncio.to_thread(list_price_alerts)
    if not alerts:
        await update.message.reply_text("🔕 no active price alerts")
        return
    lines = ["🔔 <b>Price alerts</b>"]
    for a in alerts:
        arrow = "▲" if a.direction == "above" else "▼"
        lines.append(f"#{a.id} {fmt._esc(a.ticker)} {arrow} {a.direction} {a.price:.2f}")
    lines.append("<i>/alert del ID to remove</i>")
    await update.message.reply_text("\n".join(lines))


async def _notify_alert(bot, alert, price: float) -> None:
    """Push the owner a fired-alert message (fail-open)."""
    arrow = "▲" if alert.direction == "above" else "▼"
    text = (f"🔔 <b>Price alert</b> — {fmt._esc(alert.ticker)} {arrow} "
            f"{alert.direction} {alert.price:.2f}  ·  now <b>{price:.2f}</b>")
    try:
        await bot.send_message(chat_id=OWNER_ID, text=text)
    except Exception as exc:
        logger.warning("[alerts] notify failed for #%s — %s", getattr(alert, "id", "?"), exc)


async def _alert_poll_once(bot) -> None:
    """One price-alert sweep: fetch each watched ticker's live price once, then fire +
    deactivate any alert whose target has been crossed. Fail-open per alert."""
    from persistence.db import deactivate_price_alert, list_price_alerts
    try:
        alerts = await asyncio.to_thread(list_price_alerts)
    except Exception as exc:
        logger.debug("[alerts] list failed — %s", exc)
        return
    prices: dict[str, float | None] = {}
    for a in alerts:
        try:
            tk = a.ticker.upper()
            if tk not in prices:
                prices[tk] = await asyncio.to_thread(_resolve_exit_price, a.ticker)
            px = prices[tk]
            if px is None or not _alert_crossed(a.direction, a.price, px):
                continue
            await _notify_alert(bot, a, px)
            await asyncio.to_thread(deactivate_price_alert, a.id, fired=True)
        except Exception:
            logger.exception("[alerts] failed handling alert #%s", getattr(a, "id", "?"))


async def _alert_poll_loop(app) -> None:
    """Background loop: every _ALERT_POLL_INTERVAL_S during RTH, sweep price alerts."""
    logger.info("[alerts] poller started (interval=%ds)", _ALERT_POLL_INTERVAL_S)
    while True:
        try:
            if _us_market_open_now():
                await _alert_poll_once(app.bot)
        except Exception:
            logger.exception("[alerts] poll iteration failed")
        await asyncio.sleep(_ALERT_POLL_INTERVAL_S)


async def _on_startup(app) -> None:
    """PTB post_init: launch the background price-alert poller on the app's loop."""
    app.create_task(_alert_poll_loop(app))


def build_application(token: str) -> Application:
    """Build a PTB Application with all handlers registered (no network yet)."""
    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(_on_startup)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("pos", cmd_pos))
    app.add_handler(CommandHandler("recalc", cmd_recalc))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CallbackQueryHandler(_route))
    app.add_handler(MessageHandler(filters.ALL, _on_message))
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
