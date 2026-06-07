"""
Telegram configuration parsed from the `telegram:` block of settings.yaml.

All keys are optional with safe defaults (master switch OFF), so a scan with no
`telegram:` block — or python-telegram-bot not installed — behaves exactly as
before. Token / chat id live in `config/secrets.env` (TG_BOT_TOKEN / TG_CHAT_ID),
never in YAML.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fired-signal categories the push may send, by SignalResult.direction.
_DEFAULT_ALERT_TYPES: tuple[str, ...] = ("long_entry", "exit_long", "short_entry", "exit_short")


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False           # master switch for the push hook in main.py
    daemon_enabled: bool = False    # when True, push alerts attach inline buttons (daemon answers them)
    parse_mode: str = "HTML"        # HTML | MarkdownV2
    send_stand_down: bool = False   # send a "no signals today" message when empty
    compact: bool = False           # media-group (no buttons) instead of per-signal photos
    alert_types: tuple[str, ...] = _DEFAULT_ALERT_TYPES
    mute: tuple[str, ...] = ()      # ticker blocklist (upper-cased)


def load_telegram_config(settings: dict | None) -> TelegramConfig:
    """Build a TelegramConfig from the parsed settings.yaml dict (or None)."""
    tg = ((settings or {}).get("telegram") or {})
    at = tg.get("alert_types")
    mute = tg.get("mute")
    return TelegramConfig(
        enabled=bool(tg.get("enabled", False)),
        daemon_enabled=bool(tg.get("daemon_enabled", False)),
        parse_mode=str(tg.get("parse_mode", "HTML")),
        send_stand_down=bool(tg.get("send_stand_down", False)),
        compact=bool(tg.get("compact", False)),
        alert_types=tuple(at) if isinstance(at, (list, tuple)) and at else _DEFAULT_ALERT_TYPES,
        mute=tuple(str(s).upper() for s in mute) if isinstance(mute, (list, tuple)) else (),
    )
