"""
Position-management CLI.

Subcommands
    list                                      show all positions
    open    TICKER PRICE [--stop ...] [--date ...] open a new long
    close   ID PRICE                          close an open position
    stop    ID PRICE                          update stop on an open position

Examples
    python position_CLI.py list
    python position_CLI.py open NVDA 142.55 --stop 134.00 --notes "TFSA"
    python position_CLI.py open NVDA 138.10 --date 2026-05-28   # retroactive
    python position_CLI.py close 7 8.20
    python position_CLI.py stop  3 35.00
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config" / "secrets.env")

sys.path.insert(0, str(Path(__file__).parent / "src"))

from core import position_manager as pm  # noqa: E402
from exceptions import ValidationError  # noqa: E402


def _iso_date(s: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string, rejecting future dates.

    Backs ``open --date`` for retroactive opens: an entry fill is a past
    (or same-day) event, so a future date is almost always a typo.
    """
    try:
        d = date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r} - expected ISO YYYY-MM-DD (e.g. 2026-05-28)")
    if d > date.today():
        raise argparse.ArgumentTypeError(
            f"date {s} is in the future - entry fills can't be post-dated")
    return d


def _cmd_list(_args: argparse.Namespace) -> int:
    rows = pm.list_all()
    if not rows:
        print("(no positions)")
        return 0

    print(f"{'id':>4}  {'ticker':<10}  {'side':<5}  {'entry':>10}  "
          f"{'opened':<10}  {'stop':>10}  {'exit':>10}  {'closed':<10}  notes")
    print("─" * 100)
    for p in rows:
        stop_s = f"{p.stop_price:.4f}" if p.stop_price is not None else "—"
        exit_s = f"{p.exit_price:.4f}" if p.exit_price is not None else "—"
        closed_s = p.exit_date.isoformat() if p.exit_date else "open"
        print(f"{p.id:>4}  {p.ticker:<10}  {p.side:<5}  {p.entry_price:>10.4f}  "
              f"{p.entry_date.isoformat():<10}  {stop_s:>10}  {exit_s:>10}  "
              f"{closed_s:<10}  {p.notes}")
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    entry_date = args.date or date.today()
    try:
        new_id = pm.open_position(
            ticker=args.ticker,
            entry_price=args.price,
            entry_date=entry_date,
            side=args.side,
            stop_price=args.stop,
            notes=args.notes,
        )
    except ValidationError as exc:
        print(f"✗ rejected: {exc.detail}")
        return 1
    if new_id is None:
        print("✗ failed to open position (see log)")
        return 1
    print(f"✓ opened id={new_id}  {args.side.upper()} {args.ticker.upper()} "
          f"@ {args.price:.4f} on {entry_date.isoformat()}")
    if args.stop is None:
        print("  ⚠ no stop recorded — set one with `stop` so the position can be scored")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    ok = pm.close_position(args.id, args.price, date.today())
    if not ok:
        print(f"✗ failed to close id={args.id} (already closed or not found)")
        return 1
    print(f"✓ closed id={args.id} @ {args.price:.4f}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    ok = pm.update_stop(args.id, args.price)
    if not ok:
        print(f"✗ failed to update stop on id={args.id}")
        return 1
    print(f"✓ stop updated on id={args.id} → {args.price:.4f}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="position_CLI", description="TradAlert positions CLI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list all positions").set_defaults(func=_cmd_list)

    p_open = sub.add_parser("open", help="open a new position")
    p_open.add_argument("ticker", type=str, help="Ticker symbol, e.g. AAPL.")
    p_open.add_argument("price", type=float, help="Entry fill price.")
    # The CLI accepts a short side so manual short positions can be tracked; the
    # signal/exit engine and backtester handle shorts end-to-end. Short orders
    # still require the broker to permit short selling.
    p_open.add_argument("--side", choices=("long", "short"), default="long",
                        help="long (buy-then-sell, default) or short "
                             "(sell-then-cover). Short orders require the broker "
                             "to permit short selling.")
    p_open.add_argument("--stop", type=float, default=None,
                        help="Initial stop-loss price. Optional; omit to open "
                             "without a recorded stop.")
    p_open.add_argument("--date", type=_iso_date, default=None, metavar="YYYY-MM-DD",
                        help="Entry/fill date in ISO format (default: today). Use "
                             "to backfill a retroactive open; must not be in the "
                             "future.")
    p_open.add_argument("--notes", type=str, default="",
                        help="Free-text note stored with the position.")
    p_open.set_defaults(func=_cmd_open)

    p_close = sub.add_parser("close", help="close an open position")
    p_close.add_argument("id", type=int, help="Position id (from `list`).")
    p_close.add_argument("price", type=float, help="Exit fill price.")
    p_close.set_defaults(func=_cmd_close)

    p_stop = sub.add_parser("stop", help="update stop on an open position")
    p_stop.add_argument("id", type=int, help="Position id (from `list`).")
    p_stop.add_argument("price", type=float, help="New stop-loss price.")
    p_stop.set_defaults(func=_cmd_stop)

    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    sys.exit(args.func(args))
