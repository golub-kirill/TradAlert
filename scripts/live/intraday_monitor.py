"""
Intraday 1h held-position monitor — flags a held LONG breaking down midday.

Runs hourly during market hours (Windows Task; scripts/setup/register_intraday_monitor.ps1).
For each OPEN long, fetches 1h bars and alerts (Telegram) when the last COMPLETED 1h
bar closes below the position's stop — a midday heads-up before the EOD scan.
Journal-only: alerting only, never places an order. Shorts are excluded.

Dedup: one alert per breakdown episode, re-armed once the position recovers to/above
its stop (state in data/intraday_monitor_state.json). Live-only — the backtester
never imports this.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

logger = logging.getLogger("intraday_monitor")

_STATE_PATH = _ROOT / "data" / "intraday_monitor_state.json"


# ── pure logic (unit-tested; no IO) ───────────────────────────────────────────

def is_breakdown(side: str, stop_price, last_close) -> bool:
    """True when a LONG's last completed 1h close is below its stop."""
    if side != "long" or stop_price is None:
        return False
    try:
        return float(last_close) < float(stop_price)
    except (TypeError, ValueError):
        return False


def last_completed_close(df, now):
    """(bar_start_iso, close) of the last COMPLETED 1h bar (bar_start + 1h ≤ now), or None.

    Excludes the still-forming current hour so a partial bar can't false-trigger.
    """
    if df is None or len(df) == 0:
        return None
    import pandas as pd
    hour = pd.Timedelta(hours=1)
    done = [(ts, row) for ts, row in df.iterrows() if ts + hour <= now]
    if not done:
        return None
    ts, row = done[-1]
    return ts.isoformat(), float(row["close"])


def _alert_text(pos, close: float, bar_iso: str) -> str:
    hh = bar_iso[11:16] if len(bar_iso) >= 16 else bar_iso
    return (f"⚠️ <b>{pos.ticker}</b> #{pos.id} intraday breakdown — "
            f"1h close {close:.2f} &lt; stop {float(pos.stop_price):.2f} (bar {hh})")


def evaluate(positions, closes: dict, state: dict):
    """Decide alerts + the next dedup state.

    positions : open LONG position objects (id, ticker, side, stop_price).
    closes    : {position_id: (bar_iso, close)} — last completed 1h close per position.
    state     : {str(position_id): last_alerted_bar_iso}.
    Returns (alerts: list[str], new_state: dict): one alert per breakdown episode,
    re-armed (state cleared) once the position recovers to/above its stop; state for
    positions no longer open is pruned.
    """
    open_ids = {str(p.id) for p in positions}
    new_state = {k: v for k, v in state.items() if k in open_ids}
    alerts: list[str] = []
    for p in positions:
        pid = str(p.id)
        c = closes.get(p.id)
        if c is None:
            continue
        bar_iso, close = c
        if is_breakdown(p.side, p.stop_price, close):
            if pid not in new_state:                 # first bar of this episode
                new_state[pid] = bar_iso
                alerts.append(_alert_text(p, close, bar_iso))
        else:
            new_state.pop(pid, None)                 # recovered → re-arm
    return alerts, new_state


# ── state IO ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        logger.warning("[intraday] state write failed — %s", exc)


# ── main (IO) ──────────────────────────────────────────────────────────────────

def _market_open_now() -> bool:
    """Rough RTH gate — NYSE weekday 09:30–16:00 ET (fail-open True on tz error)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= m <= 16 * 60


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="intraday_monitor",
                                 description="Intraday 1h held-long breakdown monitor.")
    ap.add_argument("--force", action="store_true", help="run even outside market hours")
    ap.add_argument("--dry-run", action="store_true", help="log alerts, don't send Telegram")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / "config" / "secrets.env")
    except Exception:
        pass

    if not args.force and not _market_open_now():
        logger.info("[intraday] market closed — skipping (use --force to override)")
        return 0

    import pandas as pd
    import yaml
    from core.fetchers.yf_fetchOne import fetch as fetch_one
    from core.position_manager import load_open_positions

    try:
        settings = yaml.safe_load((_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    except Exception:
        settings = {}

    positions = [p for p in load_open_positions().values() if p.side == "long"]
    if not positions:
        logger.info("[intraday] no open long positions")
        return 0

    start = (date.today() - timedelta(days=5)).isoformat()   # intraday ≤ 60d
    closes: dict = {}
    for p in positions:
        try:
            df = fetch_one(p.ticker, start=start, interval="1h")
            if df is None or len(df) == 0:
                continue
            idx_tz = getattr(df.index, "tz", None)
            now = pd.Timestamp.now(tz=idx_tz) if idx_tz is not None else pd.Timestamp.now()
            c = last_completed_close(df, now)
            if c is not None:
                closes[p.id] = c
        except Exception as exc:
            logger.warning("[intraday] fetch/parse failed for %s — %s", p.ticker, exc)

    alerts, new_state = evaluate(positions, closes, load_state())

    if alerts and not args.dry_run:
        from core.telegram.push import send_notice
        for text in alerts:
            send_notice(text, settings)
    for text in alerts:
        logger.info("[intraday] ALERT %s", text)
    if not alerts:
        logger.info("[intraday] no breakdowns across %d long(s)", len(positions))

    save_state(new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
