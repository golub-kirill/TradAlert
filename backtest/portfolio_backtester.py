"""
Portfolio-aware bar-replay backtester with an open-risk budget cap.

Slippage   : apply_stop_fill() (backtester.py) handles gap-through stops; entry
             slippage applied as PortfolioConfig.entry_slippage_pct.
Regime     : computed once per bar from market_t / vix_t and reused across all
             per-bar engine calls.
Commission : commission_r subtracted from r_multiple in _close_trade.

Per-bar pipeline (in order)
────────────────────────────────
    1  Pending exits fill at open (frees slots before entries compete).
    2  Pending entries fill at open in queue (scan) order, cap respected.
    3  Stop/target check on held trades against bar H/L.
    4  Engine.signal at close — queues exits and entries for next bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING, Optional

import numpy as np
import pandas as pd

from backtest.backtester import (
    _apply_dynamic_stop,
    _attach_indicators,
    _close_trade,
    adjust_target_for_slippage,
    apply_stop_fill,
    apply_stop_fill_short,
    apply_target_fill,
    apply_target_fill_short,
    call_engine_slice,
)
from backtest.earnings_history import get_earnings_history
from backtest.trade import Trade
from core.fetchers.earnings_history_store import get_earnings_events
from core.pead import EarningsEvent
from core.exits import max_hold_exit_due
from core.filter_engine import FilterEngine, SignalResult
from persistence.cache import load as cache_load

if TYPE_CHECKING:
    from core.ticker_health import TickerHealth

logger = logging.getLogger(__name__)


# ── config + result types ────────────────────────────────────────────────────

@dataclass
class PortfolioConfig:
    """
    Configuration for one portfolio-capped backtest run.

    Attributes
    ----------
    max_open_risk        : Aggregate open-risk budget, in size_mult units. Each
                           open position consumes its own ``size_mult`` (full-size =
                           1.0; a regime/chronic-reduced 0.25× position = 0.25). A new
                           entry is dropped when it would push total open risk past
                           this budget. Universe-agnostic risk control (independent of
                           watchlist size). Must be > 0. Budget B ≈ B full-size
                           positions.
    start_date           : Earliest entry date. None → warmup end.
    end_date             : Latest ENTRY date (no trade fills after it). None → end
                           of data. Bars continue past it for ``resolve_tail_bars``
                           so already-open positions exit on the real ladder — see
                           that field.
    earnings_aware       : Reconstruct historical earnings for buffer gate.
    close_open_at_eod    : Force-close open trades at last bar.
    entry_slippage_pct   : Entry fill = bar_open × (1 + this). Default 0.
    commission_r         : Per-trade commission drag in R units. Default 0.
    max_drawdown_r       : Portfolio drawdown circuit breaker (R units).
                           When cumulative R drops this far below peak, block
                           new entries until recovery to 50% of drawdown.
                           None → disabled.
    """
    max_open_risk: float
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    earnings_aware: bool = True
    close_open_at_eod: bool = True
    entry_slippage_pct: float = 0.0
    commission_r: float = 0.0
    max_drawdown_r: Optional[float] = None
    # Chronic-loser tracker (see core.ticker_health.TickerHealth). None → off,
    # baseline behavior exact. The penalty multiplies into signal.size_mult at entry.
    ticker_health: Optional["TickerHealth"] = None
    # Market-state size throttle (chop de-grossing). None → OFF, baseline
    # bit-identical. A {date: mult} mapping multiplied into entry size on the
    # ENTRY-FILL date; never touches exits or held positions. Point-in-time
    # safety is the caller's contract: every mult must derive from data STRICTLY
    # BEFORE its date (shift the source series by one session).
    size_throttle: Optional[dict] = None
    # Time-based max-hold exit (swing-horizon enforcement). None → OFF (baseline
    # bit-identical). When set, a still-open trade closes at the bar's CLOSE once
    # held ``max_hold_days`` bars (same convention as Trade.bars_held = exit_idx -
    # entry_idx). Same-bar stop/target take precedence (checked first, pessimistic).
    #   mode "hard"          → always exit at the cap.
    #   mode "if_not_profit" → exit at the cap only when not in profit at that close
    #                          (lets winners run to target).
    max_hold_days: Optional[int] = None
    max_hold_mode: str = "hard"
    # ATR trailing stop. None → OFF (baseline bit-identical). When set, current_stop
    # = highest_high − ATR×trail_atr_mult (long; short mirrors), computed at END of
    # each bar and checked on the NEXT bar (look-ahead-free). trail_activate_r: only
    # start trailing once MFE reaches this R (None → trail from entry). The R
    # denominator stays the INITIAL stop — the trail changes only exit price/reason.
    trail_atr_mult: Optional[float] = None
    trail_activate_r: Optional[float] = None
    # Breakeven stop. None → OFF. Once the trade reaches breakeven_trigger_r of
    # favorable excursion, move the stop to entry ± breakeven_buffer_atr×ATR —
    # protects the downside WITHOUT capping the upside (does not trail further). R
    # denominator stays the INITIAL stop.
    breakeven_trigger_r: Optional[float] = None
    breakeven_buffer_atr: Optional[float] = None
    # Correlation-aware open-risk budget. False → OFF (baseline bit-identical): the
    # budget compares max_open_risk against the raw sum of open size_mult. True → the
    # budget compares against the correlation-adjusted effective risk sqrt(wᵀCw),
    # where C is the clipped pairwise daily-return correlation of the open book +
    # candidate over correlation_lookback_days (negatives, sub-floor, and pairs with
    # fewer than correlation_min_overlap overlapping returns count as 0; diagonal 1).
    # Uncorrelated names get the sqrt diversification discount; a correlated cluster
    # collapses toward one shared budget slot. Effective risk ≤ Σ size_mult for
    # ρ∈[0,1], so enabling never counts a book as riskier than the raw budget would.
    correlation_cap: bool = False
    correlation_lookback_days: int = 60
    correlation_min_overlap: int = 40
    correlation_floor: float = 0.0
    # Bars to keep walking PAST end_date so positions opened inside the window
    # exit on the real ladder (stop/target/engine/time) instead of being force-
    # closed at the edge. Only tickers holding a position are walked in the tail,
    # and the walk stops as soon as the book is flat, so the cost is proportional
    # to open positions rather than universe size.
    #
    # Truncating at end_date biases SHORT windows hardest — the force-closed count
    # is ~the open-position count regardless of window length, so a 1-year window
    # loses a ~3x larger share of its trades than a 3-year one. That asymmetry
    # falls straight onto the walk-forward IS-vs-OOS degradation measure, which is
    # the one quantity walk-forward exists to produce.
    #
    # 0 → legacy truncation (force-close at end_date). No effect when end_date is
    # None: the walk already ends with the data, and close_open_at_eod there is the
    # correct terminal behaviour rather than an artifact.
    resolve_tail_bars: int = 252
    # Purge (walk-forward): drop trades whose exit lands on/after this date from
    # the RESULT, because their outcome overlaps the out-of-sample block and would
    # otherwise leak into in-sample statistics and config selection. The walk
    # itself stays causal — only the reported observations are filtered.
    # None → no purge (default; the headline and every full-range run).
    #
    # Only meaningful together with resolve_tail_bars > 0: under legacy truncation
    # nothing survives past end_date, so there is nothing to purge. The two are one
    # change — letting trades resolve is what creates the overlap the purge removes.
    purge_exit_from: Optional[date] = None
    # Exit-side slippage. 0.0 → OFF (baseline bit-identical): exits fill at the
    # modeled price exactly, which is a KNOWN optimism (measured on the pinned
    # snapshot 2026-07-11: symmetric 0.002 costs −58.7R / −0.29 Sharpe). When set,
    # every MARKET-type exit fill — stop (incl. trail/breakeven), engine_exit
    # (next-bar open), time_stop (bar close), open_eod (last close) — is worsened
    # by this fraction: long sells ×(1−slip), short covers ×(1+slip). Target fills
    # are limit orders and stay exact.
    exit_slippage_pct: float = 0.0


@dataclass
class CappedSignal:
    """An entry signal dropped because the portfolio was at its max_open_risk budget."""
    date: date
    ticker: str
    signal: SignalResult


@dataclass
class PortfolioResult:
    """Output of PortfolioBacktester.run_prepped()."""
    trades: list[Trade] = field(default_factory=list)
    capped_signals: list[CappedSignal] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    tickers_walked: int = 0
    bars_walked: int = 0
    # Trades removed by PortfolioConfig.purge_exit_from. Reported rather than
    # silently dropped: the purge preferentially removes LONG-held trades, and
    # under max_hold_mode="if_not_profit" long holds are the winners, so a window
    # purging a large share is one whose in-sample statistics lost right tail.
    purged_trades: int = 0
    # Positions still open when resolve_tail_bars ran out (force-closed at
    # open_eod). Non-zero means the tail cap bound and some trades are still
    # truncated — the residual of the very effect the tail removes.
    tail_truncated: int = 0


# ── drawdown circuit-breaker helper ──────────────────────────────────────────


class _DrawdownGate:
    """Cumulative-R peak tracker with breach-and-recover state machine.

    Used by run_prepped. When the drawdown from peak
    exceeds ``limit``, new entries are blocked until cumulative_r recovers
    to within ``recovery_frac * limit`` of the peak.
    """

    def __init__(self, limit: Optional[float], recovery_frac: float = 0.5):
        self.limit = limit
        self.recovery_frac = recovery_frac
        self.cumulative_r = 0.0
        self.peak = 0.0
        self.blocked = False

    @property
    def enabled(self) -> bool:
        return self.limit is not None

    def record(self, r: float) -> None:
        """Record a closed trade's r_multiple and update peak/state."""
        if not self.enabled:
            return
        self.cumulative_r += r
        if self.cumulative_r > self.peak:
            self.peak = self.cumulative_r
        # If currently blocked, check for recovery
        if self.blocked:
            recovery_target = self.peak - self.limit * self.recovery_frac
            if self.cumulative_r >= recovery_target:
                self.blocked = False
        else:
            drawdown = self.peak - self.cumulative_r
            if drawdown >= self.limit:
                self.blocked = True

    def reset_for_new_bar(self) -> None:
        """Re-evaluate breach state at the top of each bar (cheap idempotent op)."""
        if not self.enabled or self.blocked:
            return
        drawdown = self.peak - self.cumulative_r
        if drawdown >= self.limit:
            self.blocked = True


# ── per-ticker preparation ────────────────────────────────────────────────────

@dataclass
class _TickerPrep:
    """Pre-computed, walk-invariant context per ticker."""
    df: pd.DataFrame
    earnings_history: list[date]
    earnings_events: list[EarningsEvent] = field(default_factory=list)


def _prepare(
        tickers: list[str],
        ma_slow: int,
        earnings_aware: bool,
) -> tuple[dict[str, _TickerPrep], dict[str, str]]:
    """Load, enrich, and warmup-trim every ticker upfront."""
    prepped: dict[str, _TickerPrep] = {}
    skipped: dict[str, str] = {}
    indicator_cols = ["atr", "rsi", "macd", "macd_signal", "macd_hist"]

    for ticker in tickers:
        try:
            df = cache_load(ticker)
            df = _attach_indicators(df)
        except Exception as exc:
            skipped[ticker] = f"load/indicators failed: {exc}"
            continue

        ready = df[indicator_cols].notna().all(axis=1)
        if not ready.any():
            skipped[ticker] = "indicators never warm"
            continue
        df = df.loc[ready.idxmax():]

        if len(df) < ma_slow + 2:
            skipped[ticker] = f"only {len(df)} bars after warmup, need {ma_slow + 2}"
            continue

        eh: list[date] = []
        ev: list[EarningsEvent] = []
        if earnings_aware:
            try:
                eh = get_earnings_history(ticker)
            except Exception as exc:
                logger.warning("[%s] earnings history failed — %s", ticker, exc)
            try:
                ev = get_earnings_events(ticker)
            except Exception as exc:
                logger.warning("[%s] PEAD earnings events failed — %s", ticker, exc)

        prepped[ticker] = _TickerPrep(df=df, earnings_history=eh, earnings_events=ev)

    return prepped, skipped


# ── backtester ───────────────────────────────────────────────────────────────

class PortfolioBacktester:
    """
    Date-by-date walker with global concurrent-position cap.

    Parameters
    ----------
    engine  : FilterEngine. Its _today is mutated per call via try/finally.
    cfg     : PortfolioConfig with max_open_risk > 0.
    """

    def __init__(
            self,
            engine: FilterEngine,
            cfg: PortfolioConfig,
    ):
        if cfg.max_open_risk <= 0:
            raise ValueError(f"max_open_risk must be > 0, got {cfg.max_open_risk}")
        self._engine = engine
        self._cfg = cfg

    # ── ticker-health helper ──────────────────────────────────────────────

    def _record_close(self, trade: Trade) -> None:
        """Forward a closed trade to the chronic-loser tracker (no-op when
        ``cfg.ticker_health`` is None).
        """
        if self._cfg.ticker_health is None or trade.exit_date is None:
            return
        try:
            self._cfg.ticker_health.record_trade(
                trade.ticker, trade.exit_date, float(trade.r_multiple),
            )
        except Exception as exc:  # noqa: BLE001 - tracker must never break a backtest
            logger.warning(
                "[%s] ticker_health.record_trade failed (continuing): %s",
                trade.ticker, exc,
            )

    def _borrow_rate(self, ticker: str, direction: str) -> float:
        """Annual stock-borrow rate for a short on ``ticker`` (0.0 for longs).

        Reads ``signals.borrow.{per_ticker, annual_rate_default}`` off the
        engine config. Defaults to 0.0 — so the long-only baseline and any
        config without a ``borrow`` block are unchanged. ``getattr`` guards
        stub engines that have no typed ``cfg``.
        """
        if direction != "short":
            return 0.0
        cfg = getattr(self._engine, "cfg", None)
        if cfg is None:
            return 0.0
        b = cfg.signals.borrow
        per = b.per_ticker or {}
        try:
            return float(per.get(ticker, b.annual_rate_default) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ── correlation-aware open-risk helper ────────────────────────────────

    def _effective_open_risk(self, open_trades, cand_ticker, cand_mult,
                             prepped, D) -> float:
        """Correlation-adjusted open risk *including* the candidate: sqrt(wᵀ C w).

        w is the size_mult of every open position plus the candidate; C is the
        clipped pairwise correlation of trailing daily returns over
        ``correlation_lookback_days``, taken strictly before ``D`` (look-ahead
        free). Negative correlations, correlations below ``correlation_floor``,
        and pairs with fewer than ``correlation_min_overlap`` overlapping returns
        are set to 0; the diagonal is 1. For ρ∈[0,1] and w≥0 the result is
        ≤ Σw, so uncorrelated names earn the sqrt diversification discount while a
        correlated cluster collapses toward one shared budget slot — and enabling
        the cap never counts a book as riskier than the raw budget would.
        """
        weights = [float(t.size_mult) for t in open_trades.values()]
        weights.append(float(cand_mult))
        if len(weights) == 1:
            return float(cand_mult)

        look = int(self._cfg.correlation_lookback_days)
        # Integer column labels keep matrix order aligned with `weights` and are
        # collision-proof if a candidate ever shares a ticker with the open book.
        cols = {}
        for i, tk in enumerate(list(open_trades.keys()) + [cand_ticker]):
            prep = prepped.get(tk)
            if prep is None:
                cols[i] = pd.Series(dtype=float)
                continue
            closes = prep.df["close"]
            closes = closes[closes.index < D].tail(look + 1)
            cols[i] = closes.pct_change().dropna()

        corr = pd.DataFrame(cols).corr(
            min_periods=int(self._cfg.correlation_min_overlap))
        C = np.nan_to_num(corr.to_numpy(dtype=float), nan=0.0)
        np.clip(C, 0.0, 1.0, out=C)
        floor = float(self._cfg.correlation_floor)
        if floor > 0.0:
            C[C < floor] = 0.0
        np.fill_diagonal(C, 1.0)

        w = np.asarray(weights, dtype=float)
        eff_sq = float(w @ C @ w)
        return eff_sq ** 0.5 if eff_sq > 0.0 else 0.0

    # ── public API ────────────────────────────────────────────────────────

    def run_prepped(
            self,
            prepped,
            skipped,
            market_dfs=None,
            vix_df=None,
            macro_series=None,
            behavioral_data=None,
            spy_df=None,
            settings=None,
    ):
        """Run portfolio walk on pre-loaded _TickerPrep data (sweep hot-path).

        Bypasses _prepare() so the same OHLCV data is replayed N times with
        different FilterEngine configs without re-reading disk.
        """
        result = PortfolioResult(skipped=dict(skipped), tickers_walked=len(prepped))
        if not prepped:
            return result

        timeline = sorted({ts for prep in prepped.values() for ts in prep.df.index})
        if self._cfg.start_date:
            timeline = [t for t in timeline if t.date() >= self._cfg.start_date]
        # end_date is the ENTRY cutoff, not the last bar. Bars past it form the
        # resolution tail: exits still process, no entry ever fills. `tail_bars`
        # caps it; whatever is still open at the cap force-closes at open_eod.
        entry_cutoff = self._cfg.end_date
        if entry_cutoff:
            in_window = [t for t in timeline if t.date() <= entry_cutoff]
            tail_n = max(0, int(self._cfg.resolve_tail_bars or 0))
            tail = [t for t in timeline if t.date() > entry_cutoff][:tail_n]
            timeline = in_window + tail
        if not timeline:
            return result

        date_sets = {tk: set(p.df.index) for tk, p in prepped.items()}
        open_trades = {}
        pending_entries = {}
        pending_exits = set()
        bars_walked = 0

        # Drawdown gate lives in a shared helper so the behaviour is
        # testable in isolation.
        dd_gate = _DrawdownGate(self._cfg.max_drawdown_r)

        # Exit-side slippage on MARKET-type fills only (stop / engine_exit /
        # time_stop / open_eod); target fills are limit orders and stay exact.
        # 0.0 (default) returns the price untouched → baseline bit-identical.
        xslip = float(self._cfg.exit_slippage_pct or 0.0)

        def _exit_fill(price: float, direction: str) -> float:
            if not xslip:
                return float(price)
            return float(price) * ((1.0 + xslip) if direction == "short" else (1.0 - xslip))

        for bar_i, D in enumerate(timeline):
            D_date = D.date()
            # Resolution tail: past the entry cutoff. Exits only — and once the
            # book is flat nothing further can happen, so stop rather than walk
            # the remaining tail bars.
            in_tail = entry_cutoff is not None and D_date > entry_cutoff
            if in_tail and not open_trades:
                break
            active = [tk for tk in prepped if D in date_sets[tk]]
            if in_tail:
                active = [tk for tk in active if tk in open_trades]
            if not active:
                continue

            market_t = (
                {sym: mdf.loc[:D] for sym, mdf in market_dfs.items()}
                if market_dfs else None
            )
            vix_t = vix_df.loc[:D] if vix_df is not None else None
            regime = self._engine.market_regime(market_t, vix_t)

            # Enrich regime with macro state (point-in-time)
            if macro_series:
                from core.macro.regime import classify_macro_state
                macro_state = classify_macro_state(
                    macro_series, as_of=D, settings=settings,
                )
                regime = replace(regime, macro=macro_state)

            # Enrich regime with behavioral state
            if behavioral_data:
                from core.behavioral import classify_behavioral_state
                behavioral_state = classify_behavioral_state(
                    behavioral_data, settings=settings, spy_df=spy_df, as_of=D,
                )
                regime = replace(regime, behavioral=behavioral_state)

            closed_this_bar = set()

            # Pending exits at open
            for ticker in sorted(active):
                if ticker not in pending_exits or ticker not in open_trades:
                    continue
                bar = prepped[ticker].df.loc[D]
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                _close_trade(open_trades[ticker], D_date,
                             _exit_fill(float(bar["open"]), open_trades[ticker].direction),
                             "engine_exit", prepped[ticker].df.index, t_idx,
                             self._cfg.commission_r)
                closed = open_trades.pop(ticker)
                result.trades.append(closed)
                self._record_close(closed)
                dd_gate.record(closed.effective_r)
                pending_exits.discard(ticker)
                closed_this_bar.add(ticker)

            # Pending entries at open, in queue (scan) order. The iteration
            # order is pending_entries insertion order — the order signals were
            # queued on the previous bar — which decides who wins the last
            # budget slot on a contested bar. Do not re-sort.
            # Drawdown circuit breaker
            dd_gate.reset_for_new_bar()
            if in_tail:
                # Past the entry cutoff: anything still queued would fill with an
                # entry_date beyond the window, so it is discarded rather than
                # capped (it lost to the calendar, not to the risk budget).
                pending_entries.clear()
            elif dd_gate.blocked:
                # Skip entry fills but still process exits
                for ticker in [tk for tk in pending_entries if D in date_sets[tk]]:
                    result.capped_signals.append(
                        CappedSignal(D_date, ticker, pending_entries.pop(ticker))
                    )
            else:
                for ticker in [tk for tk in pending_entries if D in date_sets[tk]]:
                    if ticker in closed_this_bar:
                        pending_entries.pop(ticker, None)
                        continue
                    signal = pending_entries.pop(ticker)
                    base_mult = float(getattr(signal, "size_mult", 1.0))
                    chronic_mult = (
                        self._cfg.ticker_health.size_multiplier(ticker, D_date)
                        if self._cfg.ticker_health is not None
                        else 1.0
                    )
                    throttle_mult = (
                        float(self._cfg.size_throttle.get(D_date, 1.0))
                        if self._cfg.size_throttle is not None
                        else 1.0
                    )
                    final_mult = base_mult * chronic_mult * throttle_mult
                    if final_mult <= 0:  # regime + chronic-loser + throttle
                        result.capped_signals.append(
                            CappedSignal(D_date, ticker, signal)
                        )
                        continue
                    # Risk-budget cap (size_mult units). OFF (default): the raw sum
                    # of open size_mult — baseline bit-identical. ON: the budget is
                    # charged against the correlation-adjusted effective risk
                    # sqrt(wᵀCw), so correlated concurrent names share a budget slot.
                    if self._cfg.correlation_cap:
                        effective_risk = self._effective_open_risk(
                            open_trades, ticker, final_mult, prepped, D)
                    else:
                        effective_risk = (
                            sum(t.size_mult for t in open_trades.values())
                            + final_mult
                        )
                    if effective_risk > self._cfg.max_open_risk:
                        result.capped_signals.append(
                            CappedSignal(D_date, ticker, signal)
                        )
                        continue
                    bar = prepped[ticker].df.loc[D]
                    _is_short = (signal.direction == "short")
                    slip_mult = (1.0 - self._cfg.entry_slippage_pct) if _is_short else (
                            1.0 + self._cfg.entry_slippage_pct)
                    actual_entry = float(bar["open"]) * slip_mult
                    adj_target = adjust_target_for_slippage(
                        actual_entry,
                        float(signal.stop_price),
                        float(signal.target_price),
                        float(getattr(signal, "min_rr", 0.0) or 0.0),
                        direction=signal.direction,
                    )
                    open_trades[ticker] = Trade(
                        ticker=ticker, signal_type=signal.signal_type,
                        direction=signal.direction, entry_date=D_date,
                        entry_price=actual_entry,
                        initial_stop=float(signal.stop_price),
                        initial_target=adj_target,
                        market_regime=signal.market_regime, ticker_trend=signal.ticker_trend,
                        size_mult=final_mult,  # regime × chronic-loser × throttle
                        borrow_annual_rate=self._borrow_rate(ticker, signal.direction),
                    )

            # Stop / target on held trades
            for ticker in list(open_trades):
                if ticker in closed_this_bar or D not in date_sets[ticker]:
                    continue
                trade = open_trades[ticker]
                bar = prepped[ticker].df.loc[D]
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                b_open, b_low, b_high = float(bar["open"]), float(bar["low"]), float(bar["high"])
                trade.update_excursion(b_high, b_low)  # exit-quality instrumentation
                is_short = (trade.direction == "short")
                # Effective stop = the trailing/dynamic stop once set, else the
                # initial. This level was set at the PREVIOUS bar's end, so checking
                # it against this bar is look-ahead-free. The R denominator stays the
                # initial stop — a trail changes only the exit price/reason.
                eff_stop = trade.current_stop if trade.current_stop is not None else trade.initial_stop
                stop_reason = ((trade.current_stop_reason or "stop")
                               if trade.current_stop is not None and trade.current_stop != trade.initial_stop
                               else "stop")
                stop_hit = (b_high >= eff_stop) if is_short else (b_low <= eff_stop)
                if stop_hit:
                    fill = (apply_stop_fill_short(eff_stop, b_open)
                            if is_short
                            else apply_stop_fill(eff_stop, b_open))
                    _close_trade(trade, D_date, _exit_fill(fill, trade.direction),
                                 stop_reason,
                                 prepped[ticker].df.index, t_idx, self._cfg.commission_r)
                    closed = open_trades.pop(ticker)
                    result.trades.append(closed)
                    self._record_close(closed)
                    dd_gate.record(closed.effective_r)
                    closed_this_bar.add(ticker)
                    continue
                target_hit = (b_low <= trade.initial_target) if is_short else (b_high >= trade.initial_target)
                if target_hit:
                    fill = (apply_target_fill_short(trade.initial_target, b_open)
                            if is_short
                            else apply_target_fill(trade.initial_target, b_open))
                    _close_trade(trade, D_date, fill, "target",
                                 prepped[ticker].df.index, t_idx, self._cfg.commission_r)
                    closed = open_trades.pop(ticker)
                    result.trades.append(closed)
                    self._record_close(closed)
                    dd_gate.record(closed.effective_r)
                    closed_this_bar.add(ticker)
                    continue

                # Time-based max-hold exit (opt-in) via core.exits.max_hold_exit_due.
                # Off when max_hold_days is None.
                if self._cfg.max_hold_days is not None:
                    entry_pos = int(prepped[ticker].df.index.searchsorted(
                        pd.Timestamp(trade.entry_date)))
                    b_close = float(bar["close"])
                    if max_hold_exit_due(
                            bars_held=t_idx - entry_pos, current_close=b_close,
                            entry_price=trade.entry_price,
                            side=("short" if is_short else "long"),
                            max_hold_days=self._cfg.max_hold_days,
                            mode=self._cfg.max_hold_mode):
                        _close_trade(trade, D_date, _exit_fill(b_close, trade.direction),
                                     "time_stop",
                                     prepped[ticker].df.index, t_idx,
                                     self._cfg.commission_r)
                        closed = open_trades.pop(ticker)
                        result.trades.append(closed)
                        self._record_close(closed)
                        dd_gate.record(closed.effective_r)
                        closed_this_bar.add(ticker)

                # Still open after all exit checks → ratchet the trailing stop for
                # the NEXT bar from this bar's accumulated extremes (the level checked
                # above was set at the previous bar — look-ahead-free). Off when
                # trail_atr_mult is None, so the baseline replays unchanged.
                if (self._cfg.trail_atr_mult or self._cfg.breakeven_trigger_r is not None) \
                        and ticker in open_trades:
                    _atr = float(bar["atr"]) if pd.notna(bar["atr"]) else None
                    _apply_dynamic_stop(
                        trade, _atr, is_short,
                        trail_atr_mult=self._cfg.trail_atr_mult,
                        trail_activate_r=self._cfg.trail_activate_r,
                        breakeven_trigger_r=self._cfg.breakeven_trigger_r,
                        breakeven_buffer_atr=self._cfg.breakeven_buffer_atr,
                    )

            # Engine signal at close. A signal fires here and FILLS on the next
            # bar, so it may only be queued while that next bar is still inside
            # the entry window — otherwise the trade would carry an entry_date
            # past the cutoff. Exits are never gated: a position opened in the
            # window must be allowed to close.
            can_queue_entry = not in_tail and (
                entry_cutoff is None
                or (bar_i + 1 < len(timeline)
                    and timeline[bar_i + 1].date() <= entry_cutoff)
            )
            for ticker in active:
                if ticker in closed_this_bar:
                    continue
                bars_walked += 1
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                df_t = prepped[ticker].df.iloc[: t_idx + 1]
                held = ticker in open_trades
                held_short_flag = held and open_trades[ticker].direction == "short"
                held_long_flag = held and not held_short_flag
                signal = call_engine_slice(
                    self._engine, ticker, df_t, D_date,
                    market_t, vix_t, prepped[ticker].earnings_history,
                    held_long_flag,
                    regime=regime,
                    held_short=held_short_flag,
                    earnings_events=getattr(prepped[ticker], "earnings_events", None),
                )
                if not signal.passed:
                    continue
                if held and signal.direction in ("exit_long", "exit_short"):
                    pending_exits.add(ticker)
                elif not held and can_queue_entry and signal.direction in ("long", "short"):
                    pending_entries[ticker] = signal

        # Force-close remaining open trades. On a WINDOWED run these are the
        # positions the resolution tail could not see out (tail cap reached, or
        # the ticker ran out of bars) — the residual truncation, counted so it is
        # visible instead of assumed immaterial. Only counted when an entry cutoff
        # exists: at the true end of data a forced close is correct terminal
        # behaviour, not an artifact, so a full-range run must report 0 here.
        if open_trades and entry_cutoff is not None:
            result.tail_truncated = len(open_trades)
        if self._cfg.close_open_at_eod and open_trades:
            last_D = timeline[-1]
            for ticker, trade in list(open_trades.items()):
                tdf = prepped[ticker].df
                in_win = tdf.loc[:last_D]
                if in_win.empty:
                    continue
                last_bar = in_win.iloc[-1]
                last_date = last_bar.name.date() if hasattr(last_bar.name, "date") else last_D.date()
                _close_trade(trade, last_date,
                             _exit_fill(float(last_bar["close"]), trade.direction),
                             "open_eod",
                             tdf.index, len(in_win) - 1, self._cfg.commission_r)
                closed = open_trades.pop(ticker)
                result.trades.append(closed)
                self._record_close(closed)
                dd_gate.record(closed.effective_r)

        # Purge: a trade whose outcome resolves inside the out-of-sample block is
        # not an independent in-sample observation. Filtering the RESULT (not the
        # walk) keeps the replay causal while removing the contaminated rows from
        # every downstream statistic and from config selection.
        if self._cfg.purge_exit_from is not None:
            cutoff = self._cfg.purge_exit_from
            kept = [t for t in result.trades
                    if t.exit_date is None or t.exit_date < cutoff]
            result.purged_trades = len(result.trades) - len(kept)
            result.trades = kept

        result.bars_walked = bars_walked
        return result
