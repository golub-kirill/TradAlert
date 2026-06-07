# Telegram Integration — Plan & Status

> Canonical in-repo copy of the Telegram design (was the plan-mode file
> `~/.claude/plans/serene-beaming-castle.md`). A fresh chat should read this.

## Status (2026-06-06)
- **Phase 1 (push): SHIPPED & live.** `src/core/telegram/` (`config`, `format`, `bot`, `push`,
  `keyboards`) + `src/core/execution/adapter.py` (broker-adapter seam, journal-only) + fail-open
  `main.py` hook + `config/settings.yaml` `telegram:` block (default **off** → scan byte-identical) +
  `python-telegram-bot` dep + `secrets.env(.example)` updated. Token + `TG_CHAT_ID` are set in
  `config/secrets.env`; test alerts delivered to @foxDev_testBot / "TWD_BOT" (chat id 282614062).
  Tests: `test_telegram_format` / `test_telegram_push` / `test_execution_adapter` (suite green @ 297).
- **Message template:** the original `<pre>` "terminal" caption rendered as an ugly grey *copy code-box*,
  so it was replaced with a clean **`<blockquote>` card** (bold values + emoji labels) in
  `src/core/telegram/format.py`. **User wants this RICHER / PRETTIER / more creative** — see "Message
  formats" below for the next iteration ideas.
- **Phase 2 (interactive daemon `telegram_bot.py`): BUILT & TESTED 2026-06-08.** Owner-only PTB v22
  long-poll, single-instance lockfile, the alert/position-card buttons + commands `/positions /pos
  /recalc /open /close /stop /status /chart /scan /help`, all mutations through the broker-adapter seam,
  `/close` behind a Yes/No confirm. `tests/test_telegram_bot.py` (+8). **Not yet run live** — stop any
  other poller first (409 Conflict). See "Phase 2" below for the as-built notes.
- **Still pending:** richer/creative templates. *(The `🔎 TREND ✅ · MOM ✅ · …` factor line is lit since*
  *2026-06-07: `push._checklist` derives per-group marks from `SignalResult.checks`, the same source as*
  *the chart trigger panel, and passes them to `format_entry(checklist=)`.)*

## Context
TradAlert is a one-shot daily scanner (Task Scheduler → `main.py`) whose output is logs +
`data/screenshots/*.webp` charts. Goal: push the daily signals (rich messages + charts) to Telegram,
and later manage positions / ask status interactively. The `positions` journal the bot writes also
feeds `reconcile_fills.py` — the on-ramp to the "log real/paper fills so the live meter has data" TODO.

**Decisions (locked):** (1) push-first, daemon second; (2) `python-telegram-bot` (PTB v20+);
(3) `/scan` runs by subprocess `main.py`; (4) position handling = local journal now, behind a
broker-adapter *seam* — the bot never auto-executes real trades.

## Architecture
```
src/core/telegram/
  __init__.py     # package doc (import submodules directly; config/format are PTB-free)
  config.py       # load_telegram_config(settings) -> TelegramConfig (all keys optional, default off)
  format.py       # PURE HTML formatters (no PTB/network): format_entry/_exit/_watch_only/
                  #   _daily_header/_stand_down/_position_card. blockquote-card style.
                  #   format_entry(checklist=) renders the trigger-panel factor line when available.
  bot.py          # TelegramNotifier wrapping PTB Bot: send_message/send_photo/edit_message_caption/
                  #   answer_callback (async; use as `async with`)
  keyboards.py    # InlineKeyboardMarkup builders (markup only attached when daemon_enabled)
  push.py         # sync send_alerts(results, settings, macro_state=...): asyncio.run sender, FAIL-OPEN
src/core/execution/
  adapter.py      # ExecutionAdapter Protocol + JournalAdapter(position_manager) + get_adapter()
telegram_bot.py   # repo-root daemon entry (P2, NOT built), mirrors position_CLI.py bootstrap
```
`format.py` imports only stdlib (duck-typed inputs) → deterministic, unit-testable, reused by push & daemon.

## Message formats (RICHER/CREATIVE pass requested)
`sendPhoto` = chart `.webp` + HTML caption: **bold emoji header** + a `<blockquote>` card of
emoji-labelled, bold-valued lines (blockquote = clean indent, no copy code-box). Constraints: caption
≤1024, message ≤4096, allowed tags `<b><i><u><s><code><pre><blockquote><a>`, every value `html.escape`d.

Shipped card (entry):
```
📈 <b>JNJ</b> — LONG momentum
<blockquote>🎯 entry <b>232.77</b> → target <b>260.07</b>
🛑 stop <b>221.85</b>   ·   ⚖️ R:R <b>2.50</b>
⏳ hold 10–15d   ·   📦 size 0.80×
🌐 BULL_NORMAL · risk-on 0.75 ✅ tailwind
💼 4 open</blockquote>
```
Variants implemented: entry (long/short, short adds 🩳 borrow/HTB), exit/cover, watch-only (no chart),
daily header, stand-down, position card. The `🔎 TREND ✅ · MOM ✅ · LOC ▫️ · …` factor line is derived
from the same gate results as the chart trigger-panel (one source of truth) — dormant until that ships.

**Next iteration ideas (creative):** unicode progress bars (`▰▰▰▱▱`) for R:R and distance-to-stop;
a PnL gauge on the position card; section dividers (`━━━`); `<blockquote expandable>` to tuck detail;
tiered emoji by conviction; a MarkdownV2 variant. Keep HTML-safe + keep tests de-tagged
(`tests/test_telegram_format.py` strips tags before asserting, so styling tweaks don't break them).

## Phase 1 — push (SHIPPED)
- **Hook** in `main.py` between `_print_report` (line 236) and `_print_alpha_decay_watch`; `results` +
  `settings` + `macro_state` in scope. Import inside try/except → missing PTB dep / Telegram outage
  degrades to a log line, never breaks the scan. `telegram.enabled: false` → returns immediately.
- **Selection** (`push._select`): fired, `not watch_only`, by direction, filtered by `alert_types` + `mute`.
- **Send** (`push._send_all`): header message, then per-signal `send_photo` reusing the pipeline's chart
  `Path` (newest `SCREENSHOTS_DIR/{TICKER}_*.webp` via glob — never re-render; matplotlib not thread-safe).
  Owner-only (`TG_CHAT_ID`). `asyncio.run` once per scan; Bot pool `aclose`d.

## Phase 2 — interactive daemon `telegram_bot.py` (BUILT 2026-06-08)
- **PTB `Application` long-poll**, owner-only via a uniform `_owner_only` decorator on every command +
  the callback router (checks `effective_user.id == TG_CHAT_ID`; `CallbackQueryHandler` has no
  chat-filter registration param, so the decorator — not `filters.Chat` — is the single gate) + a
  catch-all `MessageHandler` reject. `run_polling(stop_signals=None, drop_pending_updates=True)`
  (Windows). Single-instance lockfile (`data/telegram_bot.lock`) via `msvcrt.locking` on a held handle
  (auto-released on crash → no stale lock) to avoid Telegram `409 Conflict`. Push is send-only → never
  conflicts. NOTE: another poller may still drain this bot's `getUpdates` — find/stop it before running.
- **Commands → existing fns:** `/help`; `/positions` → `position_manager.load_open_positions` → a
  **position card** each with inline `[Stop][Close][Recalc][Chart]`; `/pos ID` → single card;
  `/recalc [ID|all]` → reload latest bars (`cache.load`+`attach_indicators`), recompute unrealized R,
  distance to stop/target, days-to-time-stop (`core.exits.max_hold_exit_due`), run the engine exit check
  (`FilterEngine._signal_exit`) and flag any that *would exit now* (read-only); `/open|/close|/stop` →
  `ExecutionAdapter` (`/close` confirmed); `/status` → `build_status_summary()` (read-only helpers from
  `reconcile_fills.py` + `_print_alpha_decay_watch`); `/chart TICKER`; `/scan` →
  `asyncio.create_subprocess_exec(sys.executable, main.py)` streaming progress via throttled
  `edit_message` (that run does its own P1 push); `/today`; `/mute`.
- Wrap blocking DB/chart calls in `asyncio.to_thread`; serialize chart renders behind an `asyncio.Lock`.
- Phase-1 alert buttons (`callback_data` `open:…`/`close:…`/`chart:…`) get answered here: authorize,
  call adapter, `answer()`, edit message in place ("✅ logged"). `/close` requires a Yes/No confirm.
- Deployment: `scripts/register_telegram_bot.ps1` + `scripts/run_bot.bat` (at-logon, auto-restart),
  mirroring `register_daily_scan.ps1`; or NSSM service if it must run logged-off.

## Broker-adapter seam (decision 4)
`src/core/execution/adapter.py`: `ExecutionAdapter` Protocol (`open/close/update_stop`) + `JournalAdapter`
(delegates to `position_manager`). `get_adapter()` returns `JournalAdapter` unconditionally today. **All
handlers call the adapter, never `position_manager` directly** → a future `BrokerAdapter` slots in with
zero handler changes. No scan/signal path ever calls the adapter — only an explicit human action does.

## Config + deps + security
- `config/settings.yaml` `telegram:` block (all optional, default off): `enabled, daemon_enabled,
  parse_mode, send_stand_down, compact, alert_types[], mute[]`. Token/chat-id in `secrets.env` only.
- `requirements.txt`: `python-telegram-bot` (installed: 22.7).
- **Security:** owner allowlist everywhere; never log the token (don't log raw `Update`/bot context;
  extend `mask_api_keys_filter` for the `NNNN:AAA…` shape); `/close` confirmed; read-only cmds never mutate.

## Verification
- **Unit (no network/DB):** `test_telegram_format` (de-tagged content + escaping + caption cap +
  `checklist=` + determinism), `test_execution_adapter` (delegation + journal-only `get_adapter`),
  `test_telegram_push` (disabled→no send, selection filters, fail-open). P2: `test_telegram_bot`
  (non-owner rejected, `/close` confirm gate, build smoke). Keep `pytest tests/` green.
- **Manual:** token + numeric `TG_CHAT_ID` in `secrets.env`; `telegram.enabled: true`; `python main.py`
  → alert + chart arrives. P2: `python telegram_bot.py`; `/positions` buttons; Close → DB closed; `/scan`.
- **Safety:** with `telegram.enabled: false`, scan output/exit code unchanged.
