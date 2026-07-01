"""Positions dashboard — compact /positions table (format + routing + callbacks).

Pure / mock: no PTB server, no DB, no network. Async handlers driven via asyncio.run,
mirroring test_telegram_bot.py's duck-typed fakes.
"""
from __future__ import annotations

import asyncio
import re

import pytest

import telegram_bot as tb
from core.telegram import format as fmt

_OWNER = 4242


def _plain(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# ── format_positions_table ────────────────────────────────────────────────────

def test_positions_table_empty():
    assert "no open positions" in _plain(fmt.format_positions_table([]))


def test_positions_table_rows_render_pnl():
    rows = [
        {"ticker": "NVDA", "id": 12, "side": "long",
         "unrealized_r": 1.42, "unrealized_pct": 3.1, "to_stop_r": 0.9, "days_held": 5},
        {"ticker": "TSLA", "id": 13, "side": "short",
         "unrealized_r": -0.5, "unrealized_pct": -1.2, "to_stop_r": 1.5, "days_held": 2},
    ]
    out = _plain(fmt.format_positions_table(rows, budget_note="3.0/5.0R", realized_note="rz"))
    assert "2 open" in out
    assert "NVDA #12 L" in out and "TSLA #13 S" in out
    assert "+1.42R" in out and "-0.50R" in out
    assert "3.0/5.0R" in out                 # budget footer present
    assert "🟢" in out and "🔴" in out         # win/loss marker by sign


def test_positions_table_missing_metrics_still_lists():
    # A ticker whose live figures couldn't be computed still lists (identity only).
    out = _plain(fmt.format_positions_table([{"ticker": "IBM", "id": 9, "side": "long"}]))
    assert "IBM #9 L" in out and "1 open" in out


def test_positions_table_truncates_within_caption():
    rows = [{"ticker": f"TIK{i}", "id": i, "side": "long", "unrealized_r": 1.0,
             "unrealized_pct": 2.0, "to_stop_r": 1.0, "days_held": 3} for i in range(60)]
    out = fmt.format_positions_table(rows)
    assert len(out) <= fmt.CAPTION_LIMIT
    assert "more · /pos ID" in _plain(out)


# ── duck-typed PTB fakes ──────────────────────────────────────────────────────

class _Q:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.text_edits = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.text_edits.append(text)


class _Msg:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.texts.append(text)


class _Upd:
    def __init__(self, *, uid=_OWNER, data=None, with_msg=False, chat_id=1):
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": chat_id})()
        self.callback_query = _Q(data) if data is not None else None
        self.message = _Msg() if with_msg else None


class _Bot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.messages.append(text)


class _Ctx:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot or _Bot()


class _Pos:
    def __init__(self, pid, ticker, side="long"):
        self.id = pid
        self.ticker = ticker
        self.side = side


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(tb, "OWNER_ID", _OWNER)


# ── parse_callback (pure) ─────────────────────────────────────────────────────

def test_parse_callback_new_verbs():
    assert tb.parse_callback("posrefresh:x") == ("posrefresh", ("x",))
    assert tb.parse_callback("poscards:x") == ("poscards", ("x",))
    assert tb.parse_callback("posrefresh") is None      # arity 1 required


# ── routing + callbacks ───────────────────────────────────────────────────────

def test_cmd_positions_default_renders_table(monkeypatch):
    monkeypatch.setattr(tb, "_render_positions_table", lambda: "TABLE-TEXT")
    upd = _Upd(with_msg=True)
    asyncio.run(tb.cmd_positions(upd, _Ctx()))
    assert upd.message.texts == ["TABLE-TEXT"]


def test_cmd_positions_cards_arg_sends_cards(monkeypatch):
    monkeypatch.setattr(tb.pm, "load_open_positions",
                        lambda: {"NVDA": _Pos(1, "NVDA"), "TSLA": _Pos(2, "TSLA")})
    sent = []

    async def _fake_card(context, chat_id, pos, *, with_engine):
        sent.append(pos.ticker)
    monkeypatch.setattr(tb, "_send_position_card", _fake_card)
    upd = _Upd(with_msg=True)
    asyncio.run(tb.cmd_positions(upd, _Ctx(args=["cards"])))
    assert sent == ["NVDA", "TSLA"]
    assert upd.message.texts == []          # cards path does not send the table


def test_cb_posrefresh_edits_in_place(monkeypatch):
    monkeypatch.setattr(tb, "_render_positions_table", lambda: "REFRESHED")
    upd = _Upd(data="posrefresh:x")
    asyncio.run(tb._route(upd, _Ctx()))
    assert upd.callback_query.text_edits == ["REFRESHED"]
    assert upd.callback_query.answers[-1][0] == "refreshed"


def test_cb_poscards_sends_cards(monkeypatch):
    monkeypatch.setattr(tb.pm, "load_open_positions", lambda: {"NVDA": _Pos(1, "NVDA")})
    sent = []

    async def _fake_card(context, chat_id, pos, *, with_engine):
        sent.append(pos.ticker)
    monkeypatch.setattr(tb, "_send_position_card", _fake_card)
    upd = _Upd(data="poscards:x")
    asyncio.run(tb._route(upd, _Ctx()))
    assert sent == ["NVDA"]
