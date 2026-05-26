"""
Trade record for backtesting.

One Trade represents one closed round-trip: entry → exit. r_multiple is
computed from the ACTUAL entry price (T+1 open) against the INITIAL stop
(set at T close). The stop level itself never moves once recorded, so a
T+1 open that gapped past the stop still produces a meaningful (negative)
R rather than being discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

ExitReason = Literal["stop", "target", "engine_exit", "open_eod"]


@dataclass
class Trade:
    """
    One round-trip trade in the backtest ledger.

    Attributes
    ----------
    ticker         : Symbol.
    signal_type    : 'momentum' | 'mean_reversion'. Recorded from
                     SignalResult.signal_type at entry time.
    direction      : 'long'. Short backtesting is not implemented in
                     phase 8.
    entry_date     : T+1 — the bar the order actually filled.
    entry_price    : T+1 open.
    initial_stop   : Stop level set at T close. Frozen for the trade's life.
    initial_target : Target level set at T close. Frozen.
    exit_date      : Bar where exit was triggered.
                     stop/target → same bar (intraday H/L touch).
                     engine_exit → T+1 of the exit signal.
                     open_eod    → last bar of data, forced close at close.
    exit_price     : Stop, target, T+1 open, or last close depending on reason.
    exit_reason    : See ExitReason literal.
    bars_held      : Trading bars between entry_date and exit_date inclusive.
    r_multiple     : (exit_price - entry_price) / (entry_price - initial_stop).
                     Always reported, even on losers.
    market_regime  : Regime label at entry, e.g. 'BULL_NORMAL'.
    ticker_trend   : 'UPTREND' | 'DOWNTREND' | 'CHOP' at entry.
    entry_score    : SignalScorer confidence score 0–100 at entry bar.
                     0.0 when the backtester ran without a scorer.
    entry_score_components : Sub-score breakdown dict (component → 0–1).
                     Empty dict when scorer was not attached.
    """
    ticker: str
    signal_type: str
    direction: str
    entry_date: date
    entry_price: float
    initial_stop: float
    initial_target: float
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    bars_held: int = 0
    r_multiple: float = 0.0
    market_regime: str = ""
    ticker_trend: str = ""
    entry_score: float = 0.0
    entry_score_components: dict[str, float] = field(default_factory=dict)
    # P0-6 FIX: portfolio-level size multiplier from macro/behavioral regime.
    # The raw r_multiple is the per-unit-risk strategy edge; effective_r
    # below is what actually contributes to portfolio cumulative R.
    size_mult: float = 1.0

    # ── helpers ────────────────────────────────────────────────────────────

    @property
    def is_closed(self) -> bool:
        return self.exit_date is not None

    @property
    def is_winner(self) -> bool:
        return self.is_closed and self.r_multiple > 0

    @property
    def risk_per_share(self) -> float:
        """
        Entry-to-stop distance in price units. Zero or negative means the
        T+1 open already filled at/through the stop — r_multiple cannot be
        meaningfully computed.
        """
        return self.entry_price - self.initial_stop

    @property
    def effective_r(self) -> float:
        """R-multiple scaled by position-size multiplier (portfolio bookkeeping)."""
        return self.r_multiple * self.size_mult

    def compute_r(self) -> float:
        """
        R-multiple for the trade.

        Returns 0.0 when the trade is open or risk is non-positive (the
        T+1 open already gapped past the stop). The closure path in the
        backtester always populates exit_price first, so this is safe to
        call as the last step of close_trade().
        """
        if not self.is_closed or self.exit_price is None:
            return 0.0
        risk = self.risk_per_share
        if risk <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / risk
