"""
Telegram integration: outbound push alerts (phase 1) + interactive daemon (phase 2).

Submodules:
    config  — TelegramConfig parsed from settings.yaml `telegram:` block (no deps)
    format  — pure HTML message/caption formatters (no PTB, no network, testable)
    bot     — TelegramNotifier wrapping python-telegram-bot `Bot` (send side)
    push    — sync send_alerts() bridge called from main.py after a scan
    keyboards — inline-keyboard builders (phase 2)

`config` and `format` are import-light (stdlib only) so they load without
python-telegram-bot installed; `bot`/`push` require it. Import submodules
directly to avoid pulling PTB when you only need the formatters.
"""
