"""
Async Telegram send-side wrapper around python-telegram-bot's `Bot`.

Thin: owns no business logic, just message/photo/edit/callback plumbing shared by
the phase-1 push and the phase-2 daemon. Use as an async context manager so the
underlying httpx pool is initialised and shut down cleanly:

    async with TelegramNotifier(token, chat_id) as nf:
        await nf.send_message("…")
        await nf.send_photo(path, caption="…")
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: int, *, parse_mode: str = "HTML") -> None:
        self._chat_id = chat_id
        self._pm = (ParseMode.MARKDOWN_V2 if str(parse_mode).upper().startswith("MARKDOWN")
                    else ParseMode.HTML)
        self._bot = Bot(token)

    async def __aenter__(self) -> "TelegramNotifier":
        await self._bot.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._bot.shutdown()

    async def send_message(self, text: str, *, reply_markup=None,
                           disable_web_page_preview: bool = True) -> int:
        msg = await self._bot.send_message(
            chat_id=self._chat_id, text=text, parse_mode=self._pm,
            reply_markup=reply_markup, disable_web_page_preview=disable_web_page_preview,
        )
        return msg.message_id

    async def send_photo(self, photo_path, *, caption: str, reply_markup=None) -> int:
        with Path(photo_path).open("rb") as fh:
            msg = await self._bot.send_photo(
                chat_id=self._chat_id, photo=fh, caption=caption,
                parse_mode=self._pm, reply_markup=reply_markup,
            )
        return msg.message_id
