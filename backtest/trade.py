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

ExitReason = Literal["stop", "target", "engine_exit", "open_eod", "time_stop", "trail_stop"]


@dataclass
class Trade:
    """
    One round-trip trade in the backtest ledger.

    Attributes
    ----------
    ticker         : Symbol.
    signal_type    : 'momentum' | 'mean_reversion'. Recorded from
                     SignalResult.signal_type at entry time.
    direction      : 'long' or 'short'.
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
    # Portfolio-level size multiplier from macro/behavioral regime.
    # The raw r_multiple is the per-unit-risk strategy edge; effective_r
    # below is what actually contributes to portfolio cumulative R.
    size_mult: float = 1.0
    # Annual stock-borrow rate for SHORTS (e.g. 0.03 = 3%/yr).
    # 0.0 (default) = no borrow drag, so longs and pre-v2 runs are unchanged.
    # Set at entry from signals.borrow.*; folded into effective_r as a
    # per-trade R drag proportional to bars_held (see borrow_drag_r).
    borrow_annual_rate: float = 0.0

    # ── exit-quality instrumentation (Phase 0; zero behavior change) ──────────
    # Running intrabar extremes over the held bars, finalized to mfe_r/mae_r at
    # close. R uses the SAME initial-stop denominator as compute_r, so a future
    # dynamic stop never changes these. None until the first update_excursion.
    highest_high: float | None = None
    lowest_low: float | None = None
    mfe_r: float = 0.0          # max favorable excursion in R (>= 0)
    mae_r: float = 0.0          # max adverse excursion in R (<= 0)
    exit_vs_mfe: float | None = None  # r_multiple / mfe_r (capture fraction); None if mfe_r <= 0
    # Dynamic stop level (None → use initial_stop). A trailing/breakeven rule moves
    # this in the trade's favor only; initial_stop stays frozen so R is unchanged.
    current_stop: float | None = None

    # ── helpers ────────────────────────────────────────────────────────────

    def update_excursion(self, bar_high: float, bar_low: float) -> None:
        """Accumulate the intrabar extremes for one held bar.

        Call once per bar the trade is open (including the entry bar). Pure
        running max/min — look-ahead-free (only the current bar's H/L).
        """
        h, l = float(bar_high), float(bar_low)
        self.highest_high = h if self.highest_high is None else max(self.highest_high, h)
        self.lowest_low = l if self.lowest_low is None else min(self.lowest_low, l)

    def current_mfe_r(self) -> float:
        """Running max-favorable-excursion in R from the accumulated extremes (>=0).

        Used live during the walk for trailing-stop activation; the finalized form
        is ``mfe_r`` (set by compute_excursion_r at close)."""
        risk = self.risk_per_share
        fav = self.highest_high if self._sign > 0 else self.lowest_low
        if risk <= 0 or fav is None:
            return 0.0
        return max(0.0, self._sign * (fav - self.entry_price) / risk)

    def compute_excursion_r(self) -> None:
        """Finalize mfe_r / mae_r / exit_vs_mfe from the accumulated extremes.

        Uses the INITIAL-stop risk denominator (identical to compute_r), so these
        agree with r_multiple and never move under a dynamic stop. MFE is clamped
        >= 0 and MAE <= 0 (excursion is 0 at entry by convention). No-op when no
        bars were seen or risk is non-positive. Call AFTER r_multiple is set
        (exit_vs_mfe references it)."""
        risk = self.risk_per_share
        if risk <= 0 or self.highest_high is None or self.lowest_low is None:
            self.mfe_r = 0.0
            self.mae_r = 0.0
            self.exit_vs_mfe = None
            return
        favorable = self.highest_high if self._sign > 0 else self.lowest_low
        adverse = self.lowest_low if self._sign > 0 else self.highest_high
        self.mfe_r = max(0.0, self._sign * (favorable - self.entry_price) / risk)
        self.mae_r = min(0.0, self._sign * (adverse - self.entry_price) / risk)
        self.exit_vs_mfe = (self.r_multiple / self.mfe_r) if self.mfe_r > 0 else None

    @property
    def is_closed(self) -> bool:
        return self.exit_date is not None

    @property
    def is_winner(self) -> bool:
        return self.is_closed and self.r_multiple > 0

    @property
    def _sign(self) -> int:
        """+1 for longs, -1 for shorts. Defaults to +1 for legacy data
        where ``direction`` was never populated."""
        return -1 if self.direction == "short" else 1

    @property
    def risk_per_share(self) -> float:
        """
        Always-positive entry-to-stop distance in price units.

        For longs the stop is below entry → ``entry - stop`` is positive.
        For shorts the stop is above entry → ``stop - entry`` is positive.
        Zero or negative means the T+1 open already filled at/through the
        stop — r_multiple cannot be meaningfully computed.
        """
        return self._sign * (self.entry_price - self.initial_stop)

    def borrow_drag_r(self) -> float:
        """Stock-borrow cost for a held short, expressed in R units.

        Borrow accrues on the short notional (~entry price per share) per
        day held. Converting to R divides by the per-share risk:

            fee_per_share_per_day = entry_price × (annual_rate / 252)
            drag_R = fee_per_share_per_day × bars_held / risk_per_share

        252 (trading days/yr) keeps the unit consistent with ``bars_held``
        (trading bars). Returns 0.0 for longs, a non-positive rate, an
        open trade, or non-positive risk. v1 uses a single rate; a real
        per-symbol borrow source is a follow-on (see TODO).
        """
        if (self.direction != "short" or self.borrow_annual_rate <= 0
                or not self.is_closed):
            return 0.0
        risk = self.risk_per_share
        if risk <= 0:
            return 0.0
        daily_fee = self.entry_price * (self.borrow_annual_rate / 252.0)
        return daily_fee * max(self.bars_held, 0) / risk

    @property
    def effective_r(self) -> float:
        """R-multiple scaled by position-size multiplier, net of borrow cost.

        Both the strategy return and the borrow drag scale with position size, so
        size_mult multiplies the net (r_multiple − borrow_drag_r): a reduced-size
        short borrows proportionally fewer shares and so pays proportionally less
        borrow. ``borrow_drag_r()`` is 0.0 for longs and when ``borrow_annual_rate``
        is unset, so this equals ``r_multiple × size_mult`` for the long-only
        baseline."""
        return (self.r_multiple - self.borrow_drag_r()) * self.size_mult

    def compute_r(self) -> float:
        """
        R-multiple for the trade. Sign convention makes profitable
        shorts (exit < entry) report positive R, just like profitable
        longs (exit > entry).

        Returns 0.0 when the trade is open or risk is non-positive (the
        T+1 open already gapped past the stop). The closure path in the
        backtester always populates exit_price first, so this is safe to
        call as the last step of close_trade().

        Gap-through entries (risk ≤ 0) are scored 0R *by design* — this is NOT a
        hidden left-tail loss. When the T+1 open gaps past the stop, the same-bar
        stop logic fills the exit at that same open, so exit ≈ entry and the only
        realized cost is entry/exit slippage. Measured 2026-06-04: 7 of ~1098
        trades gap through, ≈ −0.25R total — roughly 1% of the headline's bootstrap
        SE (±~26R), i.e. immaterial. Booking the true slippage loss would require
        threading the *intended* (signal-based) risk through the close path; not
        worth the added surface area for 0.25R (see TODO).

        Same-bar pessimism (not a bug): when a single bar's H/L spans BOTH
        the stop and the target, the backtester records the STOP fill (the
        worse outcome). So compute_r can return a loss even on a bar that
        also touched the target — a deliberate conservative convention,
        mirrored for shorts via the sign helper.
        """
        if not self.is_closed or self.exit_price is None:
            return 0.0
        risk = self.risk_per_share
        if risk <= 0:
            return 0.0
        return self._sign * (self.exit_price - self.entry_price) / risk
