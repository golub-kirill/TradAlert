"""
Execution-adapter seam between command handlers (Telegram bot / CLI) and the
sink that records a trade.

The only sink is the local `positions` journal (`JournalAdapter`, pure DB). A
broker integration would implement the same `ExecutionAdapter` surface and slot
in via `get_adapter()` without touching any handler. The "never auto-execute"
invariant is structural: no scan/signal path calls an adapter — only an explicit
human command/button does, and `get_adapter` returns the journal-only adapter
unless a broker is deliberately opted in.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from core import position_manager
from core.position_manager import Side


@runtime_checkable
class ExecutionAdapter(Protocol):
    """The surface a bot/CLI handler calls to open/close/adjust a position."""

    def open(self, ticker: str, entry_price: float, entry_date: date,
             side: Side = "long", stop_price: float | None = None,
             notes: str = "") -> int | None: ...

    def close(self, position_id: int, exit_price: float, exit_date: date) -> bool: ...

    def update_stop(self, position_id: int, stop_price: float | None) -> bool: ...


class JournalAdapter:
    """Records to the local `positions` table only — no market interaction."""

    def open(self, ticker: str, entry_price: float, entry_date: date,
             side: Side = "long", stop_price: float | None = None,
             notes: str = "") -> int | None:
        return position_manager.open_position(
            ticker, entry_price, entry_date,
            side=side, stop_price=stop_price, notes=notes,
        )

    def close(self, position_id: int, exit_price: float, exit_date: date) -> bool:
        return position_manager.close_position(position_id, exit_price, exit_date)

    def update_stop(self, position_id: int, stop_price: float | None) -> bool:
        return position_manager.update_stop(position_id, stop_price)


def get_adapter(settings: dict | None = None) -> ExecutionAdapter:
    """Return the active execution adapter.

    Returns the journal-only adapter unconditionally. A broker adapter is
    intentionally NOT reachable from config alone — wiring one is a separate,
    deliberate change, keeping the no-auto-execution guarantee in one place.
    """
    return JournalAdapter()
