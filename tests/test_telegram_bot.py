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


# ── _load_bars: on-demand chart regen fetches FRESH bars (fail-open) ──────────

def _patch_bars(monkeypatch, *, fetch):
    import core.indicators.indicators as ind
    import persistence.cache as cache
    monkeypatch.setattr(cache, "get_or_fetch", fetch)
    monkeypatch.setattr(cache, "load", lambda t: f"CACHED:{t}")
    monkeypatch.setattr(ind, "attach_indicators", lambda df: f"IND({df})")


def test_load_bars_fresh_uses_fetch(monkeypatch):
    _patch_bars(monkeypatch, fetch=lambda t, fetcher, force=False: f"FRESH:{t}")
    assert tb._load_bars("AAPL", fresh=True) == "IND(FRESH:AAPL)"


def test_load_bars_fresh_falls_back_to_cache(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    _patch_bars(monkeypatch, fetch=boom)
    # fresh fetch failed → falls back to the cached bars so a chart still renders
    assert tb._load_bars("AAPL", fresh=True) == "IND(CACHED:AAPL)"


def test_load_bars_default_uses_cache(monkeypatch):
    def must_not_fetch(*a, **k):
        raise AssertionError("default _load_bars must not force a fetch")
    _patch_bars(monkeypatch, fetch=must_not_fetch)
    assert tb._load_bars("AAPL") == "IND(CACHED:AAPL)"


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


class _SentMsg:
    """A sent message / a reply target — only message_id matters for our tests."""
    def __init__(self, message_id):
        self.message_id = message_id


class _Message:
    def __init__(self, text=None, reply_to=None):
        self.text = text
        self.reply_to_message = _SentMsg(reply_to) if reply_to is not None else None
        self.texts = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.texts.append(("edit", text))
        return self


class _Update:
    def __init__(self, uid, *, data=None, chat_id=555, with_message=False, msg_text=None,
                 reply_to=None):
        self.effective_user = _User(uid)
        self.effective_chat = _Chat(chat_id)
        self.callback_query = _Query(data) if data is not None else None
        has_msg = with_message or msg_text is not None or reply_to is not None
        self.message = _Message(msg_text, reply_to) if has_msg else None


class _Bot:
    def __init__(self):
        self.messages = []
        self.photos = []
        self._next_id = 5000

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        mid = self._next_id
        self._next_id += 1
        self.messages.append({"chat_id": chat_id, "text": text,
                              "reply_markup": reply_markup, "message_id": mid})
        return _SentMsg(mid)

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
        self.scaled = []
        self.edits = []

    def open(self, ticker, entry_price, entry_date, side="long", stop_price=None, notes=""):
        self.opened.append((ticker, entry_price, entry_date, side, stop_price))
        return 7

    def close(self, position_id, exit_price, exit_date):
        self.closed.append((position_id, exit_price, exit_date))
        return True

    def update_stop(self, position_id, stop_price):
        self.stops.append((position_id, stop_price))
        return True

    def scale_out(self, position_id, exit_price, exit_date, fraction):
        self.scaled.append((position_id, exit_price, exit_date, fraction))
        return 11

    def edit_position(self, position_id, *, entry_price=None, stop_price=None,
                      initial_stop=None, exit_price=None, notes=None):
        fields = {k: v for k, v in dict(
            entry_price=entry_price, stop_price=stop_price, initial_stop=initial_stop,
            exit_price=exit_price, notes=notes).items() if v is not None}
        self.edits.append((position_id, fields))
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


# ── one-tap stop moves (breakeven / +1R) ─────────────────────────────────────

def test_one_r_stop_pure_long_and_short():
    long_pos = Position(id=1, ticker="A", side="long", entry_price=100.0,
                        entry_date=date.today(), stop_price=95.0, initial_stop=90.0)
    # +1R locks one risk unit (entry - initial_stop = 10) above entry → 110.
    assert tb._one_r_stop(long_pos) == 110.0
    short_pos = Position(id=2, ticker="B", side="short", entry_price=100.0,
                         entry_date=date.today(), stop_price=105.0, initial_stop=110.0)
    # short risk unit = 10 → +1R is 10 BELOW entry → 90.
    assert tb._one_r_stop(short_pos) == 90.0
    # falls back to stop_price when initial_stop is absent (legacy row)
    legacy = Position(id=3, ticker="C", side="long", entry_price=100.0,
                      entry_date=date.today(), stop_price=96.0)
    assert tb._one_r_stop(legacy) == 104.0
    # degenerate / missing stop → None (no risk unit to project)
    no_stop = Position(id=4, ticker="D", side="long", entry_price=100.0,
                       entry_date=date.today())
    assert tb._one_r_stop(no_stop) is None


def test_stop_market_note_warns_only_when_breached():
    assert tb._stop_market_note("long", 140.0, 150.0) == ""        # price above stop → ok
    assert "stop out" in tb._stop_market_note("long", 140.0, 138.0)  # price below → warn
    assert tb._stop_market_note("short", 90.0, 80.0) == ""          # price below stop → ok
    assert "stop out" in tb._stop_market_note("short", 90.0, 95.0)   # price above → warn
    assert tb._stop_market_note("long", 140.0, None) == ""          # no price → silent


def test_parse_callback_stop_moves():
    assert tb.parse_callback("stopbe:5") == ("stopbe", ("5",))
    assert tb.parse_callback("stop1r:5") == ("stop1r", ("5",))
    assert tb.parse_callback("stopbe") is None        # needs an id


def test_cb_stopbe_moves_stop_to_entry(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))  # entry 140
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 150.0)         # above → no warn

    asyncio.run(tb._route(_Update(uid=_OWNER, data="stopbe:5"), _Context()))
    assert adapter.stops == [(5, 140.0)]                                    # stop = entry


def test_cb_stop1r_locks_one_r(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))  # entry 140, stop 134
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 150.0)

    asyncio.run(tb._route(_Update(uid=_OWNER, data="stop1r:5"), _Context()))
    assert adapter.stops == [(5, 146.0)]                                    # 140 + (140-134)


def test_cb_stop1r_rejects_when_no_risk_unit(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    no_stop = Position(id=9, ticker="Z", side="long", entry_price=100.0,
                       entry_date=date.today())
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: no_stop)

    upd = _Update(uid=_OWNER, data="stop1r:9")
    asyncio.run(tb._route(upd, _Context()))
    assert adapter.stops == []                                              # nothing moved
    assert any("can't compute +1R" in a[0] for a in upd.callback_query.answers)


def test_cb_stopbe_warns_when_below_market(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))  # entry 140
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 138.0)         # below entry → warn

    upd = _Update(uid=_OWNER, data="stopbe:5")
    asyncio.run(tb._route(upd, _Context()))
    assert adapter.stops == [(5, 140.0)]                                    # still moved
    assert upd.callback_query.answers[-1][1] is True                       # show_alert (warned)


# ── edit a journaled position (command + ✏️ Edit button) ─────────────────────

def test_parse_callback_edit_shapes():
    assert tb.parse_callback("editmenu:11") == ("editmenu", ("11",))
    assert tb.parse_callback("edit:entry:11") == ("edit", ("entry", "11"))
    assert tb.parse_callback("edit:11") is None          # missing field


def test_cmd_edit_updates_entry(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    upd = _Update(uid=_OWNER, with_message=True)
    asyncio.run(tb.cmd_edit(upd, _Context(args=["11", "entry", "329.99"])))
    assert adapter.edits == [(11, {"entry_price": 329.99})]
    assert any("329.99" in t for t in upd.message.texts)


def test_cmd_edit_unknown_field(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    upd = _Update(uid=_OWNER, with_message=True)
    asyncio.run(tb.cmd_edit(upd, _Context(args=["11", "colour", "red"])))
    assert adapter.edits == []
    assert any("unknown field" in t for t in upd.message.texts)


def test_cmd_edit_non_owner_dropped(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    upd = _Update(uid=999, with_message=True)
    asyncio.run(tb.cmd_edit(upd, _Context(args=["11", "entry", "329.99"])))
    assert adapter.edits == [] and upd.message.texts == []      # stranger: no edit, no reply


def test_cmd_edit_surfaces_validation_error(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)

    class _Reject:
        def edit_position(self, *a, **k):
            raise ValidationError("initial stop 340 must be below entry 332 for a long")

    monkeypatch.setattr(tb, "get_adapter", lambda: _Reject())
    upd = _Update(uid=_OWNER, with_message=True)
    asyncio.run(tb.cmd_edit(upd, _Context(args=["11", "initial", "340"])))
    assert any("must be below entry" in t for t in upd.message.texts)


def test_cb_editmenu_open_offers_entry_stop_notes(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    upd = _Update(uid=_OWNER, data="editmenu:5")
    asyncio.run(tb._route(upd, _Context()))
    datas = [b.callback_data for row in upd.callback_query.markup_edits[-1].inline_keyboard
             for b in row]
    assert {"edit:entry:5", "edit:stop:5", "edit:notes:5"} <= set(datas)
    assert "edit:exit:5" not in datas                  # exit not editable on an open position


def test_cb_editmenu_closed_offers_exit(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    closed = Position(id=6, ticker="ZZZ", side="long", entry_price=100.0,
                      entry_date=date.today(), stop_price=90.0, initial_stop=90.0,
                      exit_price=120.0, exit_date=date.today())
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: closed)
    upd = _Update(uid=_OWNER, data="editmenu:6")
    asyncio.run(tb._route(upd, _Context()))
    datas = [b.callback_data for row in upd.callback_query.markup_edits[-1].inline_keyboard
             for b in row]
    assert "edit:exit:6" in datas and "edit:stop:6" not in datas


def test_cb_edit_prompts_then_reply_applies(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    tb._PENDING_EDIT.clear()

    ctx = _Context()
    asyncio.run(tb._route(_Update(uid=_OWNER, data="edit:entry:5"), ctx))
    prompt_id, pid, col = tb._PENDING_EDIT[555]
    assert (pid, col) == (5, "entry_price")
    assert ctx.bot.messages and "Reply with the new" in ctx.bot.messages[0]["text"]

    reply = _Update(uid=_OWNER, msg_text="329.99", reply_to=prompt_id)
    asyncio.run(tb._on_message(reply, _Context()))
    assert adapter.edits == [(5, {"entry_price": 329.99})]
    assert 555 not in tb._PENDING_EDIT


def test_pending_fill_and_edit_no_crosstalk(monkeypatch):
    # The safety fix: with BOTH a pending fill and a pending edit, a reply resolves
    # ONLY the prompt it answers — an edit reply must never open a new position.
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    tb._PENDING_FILL.clear()
    tb._PENDING_EDIT.clear()

    ctx = _Context()   # one bot → monotonic, distinct prompt message ids (as live)
    asyncio.run(tb._route(_Update(uid=_OWNER, data="fill:cust:NVDA:88.4:84.0:long"), ctx))
    asyncio.run(tb._route(_Update(uid=_OWNER, data="edit:entry:5"), ctx))
    fill_prompt = tb._PENDING_FILL[555][0]
    edit_prompt = tb._PENDING_EDIT[555][0]
    assert fill_prompt != edit_prompt                 # distinct prompts

    # reply to the EDIT prompt → applies the edit, NOT a new fill
    asyncio.run(tb._on_message(_Update(uid=_OWNER, msg_text="329.99", reply_to=edit_prompt),
                               _Context()))
    assert adapter.opened == []                       # no spurious position opened
    assert adapter.edits == [(5, {"entry_price": 329.99})]
    assert 555 not in tb._PENDING_EDIT                # edit consumed
    assert 555 in tb._PENDING_FILL                    # the unanswered fill is left intact
    tb._PENDING_FILL.clear()


def test_non_reply_message_not_consumed_by_pending(monkeypatch):
    # A free-text message that is NOT a reply to the prompt must not be hijacked.
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    tb._PENDING_FILL.clear()
    tb._PENDING_FILL[555] = (4242, "NVDA", 84.0, "long")

    reply = _Update(uid=_OWNER, msg_text="91.25")     # no reply_to → not our prompt
    asyncio.run(tb._on_message(reply, _Context()))
    assert adapter.opened == []                        # not consumed as a fill
    assert tb._PENDING_FILL.get(555) == (4242, "NVDA", 84.0, "long")   # pending intact
    assert any("Unknown input" in t for t in reply.message.texts)
    tb._PENDING_FILL.clear()


# ── closed-position card (Edit reachable on closed positions) ────────────────

def test_closed_metrics_uses_initial_stop_and_guards():
    from types import SimpleNamespace
    # realized R uses the FROZEN initial_stop (90), not the trailed stop_price (120)
    pos = SimpleNamespace(side="long", entry_price=100.0, stop_price=120.0,
                          initial_stop=90.0, exit_price=130.0,
                          entry_date=date(2026, 1, 1), exit_date=date(2026, 1, 11))
    m = tb._closed_metrics(pos)
    assert m["unrealized_r"] == pytest.approx(3.0)        # (130-100)/(100-90)
    assert m["unrealized_pct"] == pytest.approx(30.0)
    assert m["days_held"] == 10
    # no exit price → empty (nothing to realize)
    none = SimpleNamespace(side="long", entry_price=100.0, exit_price=None,
                           stop_price=90.0, initial_stop=90.0, entry_date=None, exit_date=None)
    assert tb._closed_metrics(none) == {}


def test_send_position_card_closed_has_edit_only_buttons(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    closed = Position(id=6, ticker="ZZZ", side="long", entry_price=100.0,
                      entry_date=date(2026, 1, 1), stop_price=90.0, initial_stop=90.0,
                      exit_price=130.0, exit_date=date(2026, 1, 11))
    ctx = _Context()
    asyncio.run(tb._send_position_card(ctx, 555, closed, with_engine=True))
    msg = ctx.bot.messages[-1]
    datas = {b.callback_data for row in msg["reply_markup"].inline_keyboard for b in row}
    assert datas == {"editmenu:6", "chartpos:6"}          # no live stop/close/recalc on a closed card
    assert "realized" in msg["text"]                       # realized line rendered


# ── skip / passed-on (declined fire → opportunity_tracker) ───────────────────

def test_parse_callback_skip_shapes():
    assert tb.parse_callback("skip:777:AAPL") == ("skip", ("777", "AAPL"))
    assert tb.parse_callback("skip:777:ATD.TO") == ("skip", ("777", "ATD.TO"))
    assert tb.parse_callback("skip:777") is None         # missing ticker


def test_cb_skip_marks_declined_and_disarms(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    import persistence.db as db
    seen = {}
    monkeypatch.setattr(db, "mark_declined", lambda rid, t: seen.update(skip=(rid, t)) or True)

    upd = _Update(uid=_OWNER, data="skip:777:AAPL")
    asyncio.run(tb._route(upd, _Context()))
    assert seen["skip"] == (777, "AAPL")
    assert upd.callback_query.markup_edits == [None]                 # skip button removed
    assert any("passed-on" in a[0] for a in upd.callback_query.answers)


def test_entry_actions_skip_button_only_with_run_id():
    from core.telegram.keyboards import entry_actions
    with_id = entry_actions("AAPL", 100.0, 95.0, side="long", run_id=42)
    datas = [b.callback_data for row in with_id.inline_keyboard for b in row]
    assert "skip:42:AAPL" in datas
    without = entry_actions("AAPL", 100.0, 95.0, side="long")        # back-compat
    datas2 = [b.callback_data for row in without.inline_keyboard for b in row]
    assert not any(d.startswith("skip:") for d in datas2)


# ── partial close (½ / ⅓ scale-outs) ─────────────────────────────────────────

def test_parse_callback_partial_shapes():
    assert tb.parse_callback("closemenu:5") == ("closemenu", ("5",))
    assert tb.parse_callback("partial:half:5") == ("partial", ("half", "5"))
    assert tb.parse_callback("partial:5") is None        # missing size


def test_cb_closemenu_shows_scale_picker(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    upd = _Update(uid=_OWNER, data="closemenu:5")
    asyncio.run(tb._route(upd, _Context()))
    menu = upd.callback_query.markup_edits[-1]
    datas = [b.callback_data for row in menu.inline_keyboard for b in row]
    assert {"partial:half:5", "partial:third:5", "close:5", "cancel"} <= set(datas)


def test_cb_partial_half_scales_out_via_adapter(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 150.0)
    monkeypatch.setattr(tb.pm, "remaining_fraction", lambda pid: 0.5)

    upd = _Update(uid=_OWNER, data="partial:half:5")
    asyncio.run(tb._route(upd, _Context()))
    assert adapter.scaled == [(5, 150.0, date.today(), 0.5)]
    assert any("scaled 50%" in a[0] for a in upd.callback_query.answers)


def test_cb_partial_surfaces_over_scale_rejection(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    monkeypatch.setattr(tb.pm, "get_position", lambda pid: _open_pos(pid))
    monkeypatch.setattr(tb, "_resolve_exit_price", lambda t: 150.0)

    class _Reject:
        def scale_out(self, *a, **k):
            raise ValidationError("scaling 0.5 would exceed the position (0.7 already scaled out)")

    monkeypatch.setattr(tb, "get_adapter", lambda: _Reject())
    upd = _Update(uid=_OWNER, data="partial:half:5")
    asyncio.run(tb._route(upd, _Context()))
    assert any("would exceed the position" in a[0] for a in upd.callback_query.answers)


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


# ── honest fill logging ([live | ref | custom]) ──────────────────────────────

def test_parse_callback_fill_shapes():
    assert tb.parse_callback("logmenu:XYZ:88.4000:93.1000:short") == \
        ("logmenu", ("XYZ", "88.4000", "93.1000", "short"))
    assert tb.parse_callback("fill:ref:XYZ:88.4000:93.1000:short") == \
        ("fill", ("ref", "XYZ", "88.4000", "93.1000", "short"))
    assert tb.parse_callback("fill:live:A:1.0:2.0") is None     # too few args
    assert tb.parse_callback("logmenu:XYZ:88.4:93.1") is None    # missing side


def test_entry_keyboard_opens_fill_menu(monkeypatch):
    # "Log opened" now opens the fill-source picker (logmenu), not a direct journal.
    from core.telegram.keyboards import entry_actions
    data = entry_actions("XYZ", 88.40, 93.10, side="short").inline_keyboard[0][0].callback_data
    assert tb.parse_callback(data) == ("logmenu", ("XYZ", "88.4000", "93.1000", "short"))

    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    upd = _Update(uid=_OWNER, data=data)
    asyncio.run(tb._route(upd, _Context()))
    # the card's markup is swapped to the 3-way fill picker (live / ref / custom)
    menu = upd.callback_query.markup_edits[-1]
    verbs = [b.callback_data.split(":", 2)[1] for row in menu.inline_keyboard for b in row]
    assert verbs == ["live", "ref", "cust"]


def test_cb_fill_ref_journals_at_alert_price(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    upd = _Update(uid=_OWNER, data="fill:ref:XYZ:88.4000:93.1000:short")
    asyncio.run(tb._route(upd, _Context()))
    assert adapter.opened == [("XYZ", 88.4, date.today(), "short", 93.1)]
    assert upd.callback_query.markup_edits == [None]            # buttons disarmed


def test_cb_fill_live_uses_live_quote(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb, "_live_price", lambda t: 90.25)     # real live quote
    upd = _Update(uid=_OWNER, data="fill:live:NVDA:88.4000:84.0000:long")
    asyncio.run(tb._route(upd, _Context()))
    assert adapter.opened == [("NVDA", 90.25, date.today(), "long", 84.0)]


def test_cb_fill_live_no_quote_warns_and_does_not_journal(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    monkeypatch.setattr(tb, "_live_price", lambda t: None)      # no live quote
    upd = _Update(uid=_OWNER, data="fill:live:NVDA:88.4000:84.0000:long")
    asyncio.run(tb._route(upd, _Context()))
    assert adapter.opened == []                                 # nothing journaled
    assert any("no live quote" in a[0] for a in upd.callback_query.answers)


def test_cb_fill_custom_prompts_then_reply_journals(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    tb._PENDING_FILL.clear()

    # tap "✍️ Custom" → a force-reply prompt is sent and the fill is pending (bound
    # to the prompt's message id)
    ctx = _Context()
    asyncio.run(tb._route(_Update(uid=_OWNER, data="fill:cust:NVDA:88.4000:84.0000:long"), ctx))
    prompt_id, *rest = tb._PENDING_FILL[555]
    assert rest == ["NVDA", 84.0, "long"]
    assert ctx.bot.messages and "Reply with the fill price" in ctx.bot.messages[0]["text"]

    # the owner REPLIES to that prompt with a typed price → journaled, pending cleared
    reply = _Update(uid=_OWNER, msg_text="91.25", reply_to=prompt_id)
    asyncio.run(tb._on_message(reply, _Context()))
    assert adapter.opened == [("NVDA", 91.25, date.today(), "long", 84.0)]
    assert 555 not in tb._PENDING_FILL


def test_non_owner_message_does_not_consume_pending_or_journal(monkeypatch):
    # The message path (_on_message) carries its own owner gate — a stranger's reply
    # must not consume a pending fill nor journal. Mirrors the callback-path guard test.
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    tb._PENDING_FILL.clear()
    tb._PENDING_FILL[555] = ("NVDA", 84.0, "long")

    reply = _Update(uid=999, msg_text="91.25")           # a stranger replies
    asyncio.run(tb._on_message(reply, _Context()))
    assert adapter.opened == []                           # nothing journaled
    assert tb._PENDING_FILL.get(555) == ("NVDA", 84.0, "long")  # pending NOT consumed
    assert reply.message.texts == []                     # stranger gets no reply
    tb._PENDING_FILL.clear()


def test_live_price_is_quote_only_no_cache_fallback(monkeypatch):
    # _live_price must return the live quote (or None) and NEVER fall back to the
    # stale cache — an "@ live" fill is honest or it fails.
    monkeypatch.setattr("core.fetchers.live_price.get_live_price", lambda t: 90.25)
    assert tb._live_price("NVDA") == 90.25

    def boom(t):
        raise RuntimeError("fetch down")
    monkeypatch.setattr("core.fetchers.live_price.get_live_price", boom)
    assert tb._live_price("NVDA") is None                 # fail → None, not a cached price


def test_custom_fill_bad_price_is_not_journaled(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)
    adapter = _Adapter()
    monkeypatch.setattr(tb, "get_adapter", lambda: adapter)
    tb._PENDING_FILL.clear()
    tb._PENDING_FILL[555] = (777, "NVDA", 84.0, "long")

    reply = _Update(uid=_OWNER, msg_text="not a number", reply_to=777)
    asyncio.run(tb._on_message(reply, _Context()))
    assert adapter.opened == []                                 # junk → no journal
    assert 555 not in tb._PENDING_FILL                          # one-shot consumed
    assert any("couldn't read a price" in t for t in reply.message.texts)


# ── build smoke ──────────────────────────────────────────────────────────────

def test_build_application_registers_all_handlers():
    app = tb.build_application("123456:ABCdefGhIjKlMnOpQrStUvWxYz0123456789")
    n = sum(len(g) for g in app.handlers.values())
    assert n == 13                                          # 11 commands + callback + catch-all

    cmds = set()
    for group in app.handlers.values():
        for h in group:
            if getattr(h, "commands", None):
                cmds |= set(h.commands)
    for c in ("positions", "pos", "recalc", "open", "close", "stop", "edit",
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


def test_basic_metrics_reads_typed_execution_config(monkeypatch):
    """Regression: _basic_metrics must read engine.cfg.execution (the typed config),
    not the dropped engine._cfg dict. The max-hold metrics populate without raising."""
    import pandas as pd
    from types import SimpleNamespace

    stub = SimpleNamespace(cfg=SimpleNamespace(
        execution=SimpleNamespace(max_hold_days=25, max_hold_mode="if_not_profit")))
    monkeypatch.setattr(tb, "_get_engine", lambda: stub)

    idx = pd.date_range("2025-01-01", periods=10, freq="B")
    df = pd.DataFrame({"close": [100.0] * 10}, index=idx)
    pos = SimpleNamespace(side="long", entry_price=100.0, stop_price=95.0,
                          entry_date=idx[0].date(), ticker="TEST.1")

    m = tb._basic_metrics(pos, df)
    assert m["max_hold"] == 25
    assert m["mode"] == "if_not_profit"
    assert m["time_stop_left"] == 25 - m["days_held"]


def test_basic_metrics_uses_live_now_price(monkeypatch):
    """now / live PnL use the live price (not the cached close); falls back to close."""
    import pandas as pd
    from types import SimpleNamespace

    stub = SimpleNamespace(cfg=SimpleNamespace(
        execution=SimpleNamespace(max_hold_days=25, max_hold_mode="if_not_profit")))
    monkeypatch.setattr(tb, "_get_engine", lambda: stub)

    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    df = pd.DataFrame({"close": [100.0] * 5}, index=idx)          # last daily close = 100
    pos = SimpleNamespace(side="long", entry_price=100.0, stop_price=90.0,
                          entry_date=idx[0].date(), ticker="TEST.1")

    # live price 98 (below entry) → now/PnL reflect the live tape, not the flat close
    m = tb._basic_metrics(pos, df, 98.0)
    assert m["now"] == 98.0
    assert m["unrealized_pct"] == pytest.approx(-2.0)
    assert m["unrealized_r"] == pytest.approx(-0.2)               # (98-100)/10
    assert m["to_stop_r"] == pytest.approx(0.8)                   # (98-90)/10

    # no live price → falls back to the last close → reads flat
    m2 = tb._basic_metrics(pos, df)
    assert m2["now"] == 100.0 and m2["unrealized_r"] == pytest.approx(0.0)
