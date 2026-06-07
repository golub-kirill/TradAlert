"""Interactive daemon: callback parsing, owner gate, /close confirm gate, build smoke.

No network / no DB / no pytest-asyncio: async handlers are driven via asyncio.run
with duck-typed Update/Query/Context fakes (mirrors test_telegram_push's monkeypatch
style); the adapter + position lookup are monkeypatched so nothing mutates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import telegram_bot as tb
from core.position_manager import Position

_OWNER = 100


# ── duck-typed PTB fakes ─────────────────────────────────────────────────────

class _User:
    def __init__(self, uid):
        self.id = uid


class _Chat:
    def __init__(self, cid):
        self.id = cid


class _Query:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.markup_edits = []
        self.text_edits = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)

    async def edit_message_text(self, text):
        self.text_edits.append(text)


class _Message:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.texts.append(("edit", text))
        return self


class _Update:
    def __init__(self, uid, *, data=None, chat_id=555, with_message=False):
        self.effective_user = _User(uid)
        self.effective_chat = _Chat(chat_id)
        self.callback_query = _Query(data) if data is not None else None
        self.message = _Message() if with_message else None


class _Bot:
    def __init__(self):
        self.messages = []
        self.photos = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        self.photos.append({"chat_id": chat_id, "caption": caption})


class _Context:
    def __init__(self, bot=None, args=None):
        self.bot = bot or _Bot()
        self.args = args or []


class _Adapter:
    def __init__(self):
        self.opened = []
        self.closed = []
        self.stops = []

    def open(self, ticker, entry_price, entry_date, side="long", stop_price=None, notes=""):
        self.opened.append((ticker, entry_price, entry_date, side, stop_price))
        return 7

    def close(self, position_id, exit_price, exit_date):
        self.closed.append((position_id, exit_price, exit_date))
        return True

    def update_stop(self, position_id, stop_price):
        self.stops.append((position_id, stop_price))
        return True


def _open_pos(pid=5, ticker="NVDA"):
    return Position(id=pid, ticker=ticker, side="long", entry_price=140.0,
                    entry_date=date.today(), stop_price=134.0)


# ── parse_callback (pure) ────────────────────────────────────────────────────

def test_parse_callback_valid_shapes():
    assert tb.parse_callback("open:NVDA:142.5500:134.0000") == ("open", ("NVDA", "142.5500", "134.0000"))
    assert tb.parse_callback("chart:AAPL") == ("chart", ("AAPL",))
    assert tb.parse_callback("chartpos:5") == ("chartpos", ("5",))
    assert tb.parse_callback("close:5") == ("close", ("5",))
    assert tb.parse_callback("confirm:close:5") == ("confirm", ("close", "5"))
    assert tb.parse_callback("cancel") == ("cancel", ())


def test_parse_callback_rejects_malformed():
    assert tb.parse_callback("") is None
    assert tb.parse_callback(None) is None
    assert tb.parse_callback("garbage") is None          # unknown verb
    assert tb.parse_callback("open:NVDA") is None         # too few args
    assert tb.parse_callback("confirm:close") is None     # too few args
    assert tb.parse_callback("chart:A:B") is None          # too many args


# ── owner gate ───────────────────────────────────────────────────────────────

def test_non_owner_rejected_and_no_mutation(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)

    upd = _Update(uid=999, data="close:5")            # not the owner
    asyncio.run(tb._route(upd, _Context()))

    assert upd.callback_query.answers == [("Not authorized", True)]
    assert adapter.closed == []                         # nothing touched


# ── /close confirm gate (the destructive-action guard) ───────────────────────

def test_close_button_shows_confirm_without_mutating(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)

    ctx = _Context()
    upd = _Update(uid=_OWNER, data="close:5")
    asyncio.run(tb._route(upd, ctx))

    # a confirm prompt was sent, and NOTHING was closed yet
    assert len(ctx.bot.messages) == 1
    rm = ctx.bot.messages[0]["reply_markup"]
    assert rm.inline_keyboard[0][0].callback_data == "confirm:close:5"
    assert adapter.closed == []


def test_confirm_close_executes_close(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda ticker: 150.0)

    upd = _Update(uid=_OWNER, data="confirm:close:5")
    asyncio.run(tb._route(upd, _Context()))

    assert adapter.closed == [(5, 150.0, date.today())]


# ── log-opened callback delegates to the adapter ─────────────────────────────

def test_cb_open_journals_via_adapter(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)

    upd = _Update(uid=_OWNER, data="open:NVDA:142.5500:134.0000")
    asyncio.run(tb._route(upd, _Context()))

    assert adapter.opened == [("NVDA", 142.55, date.today(), "long", 134.0)]
    assert upd.callback_query.markup_edits == [None]       # buttons disarmed
    assert upd.callback_query.answers[-1][0].startswith("✅ logged")


# ── build smoke ──────────────────────────────────────────────────────────────

def test_build_application_registers_all_handlers():
    app = tb.build_application("123456:ABCdefGhIjKlMnOpQrStUvWxYz0123456789")
    n = sum(len(g) for g in app.handlers.values())
    assert n == 12                                          # 10 commands + callback + catch-all

    cmds = set()
    for group in app.handlers.values():
        for h in group:
            if getattr(h, "commands", None):
                cmds |= set(h.commands)
    for c in ("positions", "pos", "recalc", "open", "close", "stop",
              "status", "chart", "scan", "help", "start"):
        assert c in cmds


# ── secret masking covers the bot token ──────────────────────────────────────

def test_mask_filter_masks_bot_token_and_hex():
    from core.fetchers.http import mask_api_keys_filter
    f = mask_api_keys_filter()

    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "polling https://api.telegram.org/bot123456789:AAFakeToken_secret-PART012345678/getUpdates",
                            None, None)
    f.filter(rec)
    msg = rec.getMessage()
    assert "<TG-TOKEN-MASKED>" in msg
    assert "AAFakeToken_secret" not in msg

    rec2 = logging.LogRecord("x", logging.INFO, __file__, 1,
                             "fred key=abcdef0123456789abcdef0123456789 ok", None, None)
    f.filter(rec2)
    assert "<API-KEY-MASKED>" in rec2.getMessage()
