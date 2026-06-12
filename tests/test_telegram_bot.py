"""Interactive daemon: callback parsing, owner gate, /close confirm gate, build smoke.

No network / no DB / no pytest-asyncio: async handlers are driven via asyncio.run
with duck-typed Update/Query/Context fakes (mirrors test_telegram_push's monkeypatch
style); the adapter + position lookup are monkeypatched so nothing mutates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import pytest

import telegram_bot as tb
from core.position_manager import Position
from exceptions import ValidationError

_OWNER = 100


@pytest.fixture(autouse=True)
def _no_db_budget(monkeypatch):
    """Keep open handlers DB-free: stub the budget advisory + config read."""
    monkeypatch.setattr(tb, "_max_open_risk", lambda: 5.0)
    monkeypatch.setattr(tb.pm, "open_risk_advisory", lambda *a, **k: None)


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
    assert tb.parse_callback("open:NVDA:142.5500:134.0000:long") == \
        ("open", ("NVDA", "142.5500", "134.0000", "long"))
    assert tb.parse_callback("open:XYZ:88.4000:93.1000:short") == \
        ("open", ("XYZ", "88.4000", "93.1000", "short"))
    assert tb.parse_callback("open:NVDA:142.5500:134.0000") == \
        ("open", ("NVDA", "142.5500", "134.0000"))           # legacy 3-arg (no side)
    assert tb.parse_callback("chart:AAPL") == ("chart", ("AAPL",))
    assert tb.parse_callback("chartpos:5") == ("chartpos", ("5",))
    assert tb.parse_callback("close:5") == ("close", ("5",))
    assert tb.parse_callback("confirm:close:5") == ("confirm", ("close", "5"))
    assert tb.parse_callback("cancel") == ("cancel", ())


def test_parse_callback_rejects_malformed():
    assert tb.parse_callback("") is None
    assert tb.parse_callback(None) is None
    assert tb.parse_callback("garbage") is None                   # unknown verb
    assert tb.parse_callback("open:NVDA") is None                 # too few args
    assert tb.parse_callback("open:N:1:2:3:long") is None         # too many args
    assert tb.parse_callback("confirm:close") is None             # too few args
    assert tb.parse_callback("chart:A:B") is None                  # too many args


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


# ── log-opened callback delegates to the adapter (direction-aware) ───────────

def test_cb_open_journals_long_via_adapter(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)

    upd = _Update(uid=_OWNER, data="open:NVDA:142.5500:134.0000:long")
    asyncio.run(tb._route(upd, _Context()))

    assert adapter.opened == [("NVDA", 142.55, date.today(), "long", 134.0)]
    assert upd.callback_query.markup_edits == [None]       # buttons disarmed
    assert upd.callback_query.answers[-1][0].startswith("✅ logged")


def test_cb_open_journals_short_side(monkeypatch):
    # A short entry card must journal as short (stop above entry), not long.
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)

    upd = _Update(uid=_OWNER, data="open:XYZ:88.4000:93.1000:short")
    asyncio.run(tb._route(upd, _Context()))

    assert adapter.opened == [("XYZ", 88.4, date.today(), "short", 93.1)]


def test_cb_open_surfaces_validation_rejection(monkeypatch):
    # An invalid open (e.g. inverted-risk short logged long) is rejected at the
    # data layer; the user is told the reason and the buttons stay armed.
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)

    class _Reject:
        def open(self, *a, **k):
            raise ValidationError("stop 93.1 must be below entry 88.4 for a long", ticker="AB")

    monkeypatch.setattr(tb, "get_adapter", lambda: _Reject())
    upd = _Update(uid=_OWNER, data="open:AB:88.4000:93.1000:long")
    asyncio.run(tb._route(upd, _Context()))

    assert any("must be below entry" in a[0] for a in upd.callback_query.answers)
    assert upd.callback_query.markup_edits == []           # not disarmed — open failed


def test_cb_open_legacy_3arg_defaults_long(monkeypatch):
    # Cards pushed before the side was encoded (3-arg callback) still log, as long.
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)

    upd = _Update(uid=_OWNER, data="open:ATD.TO:82.6000:78.7793")
    asyncio.run(tb._route(upd, _Context()))

    assert adapter.opened == [("ATD.TO", 82.6, date.today(), "long", 78.7793)]


def test_entry_keyboard_round_trips_through_open_handler(monkeypatch):
    # End-to-end: the keyboard the push builds for a SHORT entry must parse and
    # route to a short open — the bug that logged shorts as longs.
    from core.telegram.keyboards import entry_actions
    data = entry_actions("XYZ", 88.40, 93.10, side="short").inline_keyboard[0][0].callback_data
    assert tb.parse_callback(data) == ("open", ("XYZ", "88.4000", "93.1000", "short"))

    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    asyncio.run(tb._route(_Update(uid=_OWNER, data=data), _Context()))
    assert adapter.opened == [("XYZ", 88.4, date.today(), "short", 93.1)]


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

    # Non-hex lowercase alnum keys (rotated / third-party) are masked too
    rec3 = logging.LogRecord("x", logging.INFO, __file__, 1,
                             "key=zq7xk2m9pw4nv8rt3yb6hd1fg5js0caz end", None, None)
    f.filter(rec3)
    assert "<API-KEY-MASKED>" in rec3.getMessage()
    assert "zq7xk2m9" not in rec3.getMessage()
