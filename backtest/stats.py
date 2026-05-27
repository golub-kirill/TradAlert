"""
Aggregate statistics for backtest trade ledgers.

Two public functions:
    compute_stats(trades)        — flat aggregate over an iterable.
    group_by(trades, key)        — bucket → Stats. `key` can be either an
                                   attribute name (string) or a callable
                                   that takes a Trade and returns a string.

Profit factor is reported as float('inf') when there are no losers.
Persistence layer (core.backtest.db) clamps it for the DECIMAL column.

Max drawdown is computed in R-equity space — peak-to-trough drop in the
running sum of r_multiple over trades in the order given. Caller is
responsible for sorting (typically by exit_date or entry_date).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from backtest.trade import Trade


# ── result type ──────────────────────────────────────────────────────────────

@dataclass
class Stats:
    """
    Aggregate statistics for a list of closed trades.

    Attributes
    ----------
    trades_count   : Total closed trades counted.
    wins           : Trades where r_multiple > 0.
    losses         : Trades where r_multiple <= 0.
    win_rate       : wins / trades_count.
    avg_winner_r   : Mean r_multiple of winners only.
    avg_loser_r    : Mean r_multiple of losers only (always non-positive).
    expectancy_r   : Mean r_multiple across all trades.
    total_r        : Sum of r_multiples — cumulative R if sized equal-R.
    profit_factor  : sum(winner R) / abs(sum(loser R)).
                     float('inf') when there are no losers.
    max_drawdown_r : Largest peak-to-trough drop in cumulative R, in R units.
    best_trade_r   : Max r_multiple seen.
    worst_trade_r  : Min r_multiple seen.
    avg_bars_held  : Mean bars_held across all trades.
    """
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_winner_r: float = 0.0
    avg_loser_r: float = 0.0
    expectancy_r: float = 0.0
    total_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_r: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    avg_bars_held: float = 0.0


# ── public API ───────────────────────────────────────────────────────────────

def compute_stats(trades: Iterable[Trade]) -> Stats:
    """
    Compute aggregate statistics over a list of trades.

    Open trades (Trade.is_closed is False) are filtered out silently.
    """
    closed = [t for t in trades if t.is_closed]
    if not closed:
        return Stats()

    rs = [t.effective_r for t in closed]
    winners = [r for r in rs if r > 0]
    losers = [r for r in rs if r <= 0]
    sum_wins = sum(winners)
    sum_loss = sum(losers)

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        cumulative += r
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)

    return Stats(
        trades_count=len(closed),
        wins=len(winners),
        losses=len(losers),
        win_rate=len(winners) / len(closed),
        avg_winner_r=(sum_wins / len(winners)) if winners else 0.0,
        avg_loser_r=(sum_loss / len(losers)) if losers else 0.0,
        expectancy_r=sum(rs) / len(rs),
        total_r=sum(rs),
        profit_factor=(sum_wins / abs(sum_loss)) if losers else float("inf"),
        max_drawdown_r=max_dd,
        best_trade_r=max(rs),
        worst_trade_r=min(rs),
        avg_bars_held=sum(t.bars_held for t in closed) / len(closed),
    )


def group_by(
        trades: Iterable[Trade],
        key: str | Callable[[Trade], str],
) -> dict[str, Stats]:
    """
    Bucket closed trades by `key` and compute stats per bucket.

    `key` may be either:
        • a string attribute name, e.g. 'signal_type', 'market_regime',
          'ticker_trend', 'exit_reason', 'ticker'
        • a callable Trade → str, for derived buckets such as year or
          rolling window. Example:
              group_by(trades, lambda t: str(t.entry_date.year))

    Empty/None values are bucketed under '<empty>'.
    """
    if isinstance(key, str):
        def keyfn(t: Trade) -> str:
            return str(getattr(t, key, "") or "<empty>")
    else:
        def keyfn(t: Trade) -> str:
            try:
                return str(key(t) or "<empty>")
            except Exception:
                return "<empty>"

    buckets: dict[str, list[Trade]] = {}
    for t in trades:
        if not t.is_closed:
            continue
        buckets.setdefault(keyfn(t), []).append(t)
    return {k: compute_stats(v) for k, v in buckets.items()}
