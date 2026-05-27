"""
Portfolio-aware bar-replay backtester with concurrent-position cap.

    Slippage apply_stop_fill() from backtester.py handles gap-through
            stops. Entry slippage applied as PortfolioConfig.entry_slippage_pct.

    Scoring When a SignalScorer is injected, it enriches each entry signal
            before the signal is queued (Phase 4). Phase 2 then sorts
            contested signals by score descending — highest-confidence
            trade wins the slot when the portfolio is at cap.
            No scorer → score defaults to 0.0 for all → alphabetical
            tiebreak preserved.

    Regime  Computed once per bar from market_t / vix_t and reused
            across all Phase 4 engine calls. Saves N engine._market_regime
            calls per bar.

    Commission commission_r subtracted from r_multiple in _close_trade.

Per-bar phase order
────────────────────────────────
    Phase 1  Pending exits fill at open (frees slots before entries compete).
    Phase 2  Pending entries fill at open, highest-score first, cap respected.
    Phase 3  Stop/target check on held trades against bar H/L.
    Phase 4  Engine.signal at close — queues exits and entries for next bar.
             Entry signals enriched with scorer before queuing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING, Optional

import pandas as pd

from backtest.backtester import (
    _attach_indicators,
    _close_trade,
    adjust_target_for_slippage,
    apply_stop_fill,
    apply_stop_fill_short,
    apply_target_fill,
    apply_target_fill_short,
    call_engine_slice,
)
from backtest.earnings_history import (
    get_earnings_history,
    next_earnings_from,
)
from backtest.trade import Trade
from core.filter_engine import FilterEngine, SignalResult
from persistence.cache import load as cache_load

if TYPE_CHECKING:
    from core.scoring import SignalScorer
    from core.ticker_health import TickerHealth

logger = logging.getLogger(__name__)


# ── config + result types ────────────────────────────────────────────────────

@dataclass
class PortfolioConfig:
    """
    Configuration for one portfolio-capped backtest run.

    Attributes
    ----------
    max_concurrent       : Hard cap on open positions. Must be ≥ 1.
    start_date           : Earliest entry date. None → warmup end.
    end_date             : Latest entry date. None → end of data.
    earnings_aware       : Reconstruct historical earnings for buffer gate.
    close_open_at_eod    : Force-close open trades at last bar.
    entry_slippage_pct   : Entry fill = bar_open × (1 + this). Default 0.
    commission_r         : Per-trade commission drag in R units. Default 0.
    max_drawdown_r       : Portfolio drawdown circuit breaker (R units).
                           When cumulative R drops this far below peak, block
                           new entries until recovery to 50% of drawdown.
                           None → disabled.
    """
    max_concurrent: int
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    earnings_aware: bool = True
    close_open_at_eod: bool = True
    entry_slippage_pct: float = 0.0
    commission_r: float = 0.0
    max_drawdown_r: Optional[float] = None
    # Chronic-loser tracker (see core.ticker_health.TickerHealth). When None,
    # the policy is off and the backtester replays baseline behavior exactly.
    # The tracker's penalty multiplies into signal.size_mult at Phase 2 entry.
    ticker_health: Optional["TickerHealth"] = None


@dataclass
class CappedSignal:
    """An entry signal dropped because the portfolio was at max_concurrent."""
    date: date
    ticker: str
    signal: SignalResult


@dataclass
class PortfolioResult:
    """Output of PortfolioBacktester.run_all()."""
    trades: list[Trade] = field(default_factory=list)
    capped_signals: list[CappedSignal] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    tickers_walked: int = 0
    bars_walked: int = 0


# ── drawdown circuit-breaker helper ──────────────────────────────────────────


class _DrawdownGate:
    """Cumulative-R peak tracker with breach-and-recover state machine.

    Shared by both run_prepped and run_all. When the drawdown from peak
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
        if earnings_aware:
            try:
                eh = get_earnings_history(ticker)
            except Exception as exc:
                logger.warning("[%s] earnings history failed — %s", ticker, exc)

        prepped[ticker] = _TickerPrep(df=df, earnings_history=eh)

    return prepped, skipped


# ── backtester ───────────────────────────────────────────────────────────────

class PortfolioBacktester:
    """
    Date-by-date walker with global concurrent-position cap.

    Parameters
    ----------
    engine  : FilterEngine. Its _today is mutated per call via try/finally.
    cfg     : PortfolioConfig with max_concurrent ≥ 1.
    scorer  : Optional SignalScorer from scoring.py. When provided, every
              queued entry signal is enriched in-place (score, components,
              description) and Phase 2 selects by highest score first.
              When None, score defaults to 0.0 and order is alphabetical.
    """

    def __init__(
            self,
            engine: FilterEngine,
            cfg: PortfolioConfig,
            scorer: SignalScorer | None = None,
    ):
        if cfg.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be ≥ 1, got {cfg.max_concurrent}")
        self._engine = engine
        self._cfg = cfg
        self._scorer = scorer

    # ── ticker-health helper ──────────────────────────────────────────────

    def _record_close(self, trade: Trade) -> None:
        """Forward a closed trade to the chronic-loser tracker, if enabled.

        Safe to call when ``cfg.ticker_health`` is None — no-op. Pulled
        into a helper so the four close sites stay one-liners.
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
        stub engines that have no ``_cfg``.
        """
        if direction != "short":
            return 0.0
        cfg = getattr(self._engine, "_cfg", {}) or {}
        b = (cfg.get("signals", {}) or {}).get("borrow", {}) or {}
        per = b.get("per_ticker", {}) or {}
        try:
            return float(per.get(ticker, b.get("annual_rate_default", 0.0)) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ── public API ────────────────────────────────────────────────────────

    def run_all(
            self,
            tickers: list[str],
            market_dfs: dict[str, pd.DataFrame] | None = None,
            vix_df: pd.DataFrame | None = None,
            macro_series: dict[str, pd.DataFrame] | None = None,
            behavioral_data: dict | None = None,
            spy_df: pd.DataFrame | None = None,
            settings: dict | None = None,
    ) -> PortfolioResult:
        """
        Run the portfolio-capped backtest for the full universe.

        Context-only symbols (e.g. ^VIX) must be excluded by the caller.
        """
        ma_slow = self._engine._cfg["trend"]["ma_slow"]
        prepped, skipped = _prepare(tickers, ma_slow, self._cfg.earnings_aware)

        result = PortfolioResult(skipped=skipped, tickers_walked=len(prepped))
        if not prepped:
            return result

        # ── unified timeline ──────────────────────────────────────────────
        timeline = sorted({ts for prep in prepped.values() for ts in prep.df.index})
        if self._cfg.start_date:
            timeline = [t for t in timeline if t.date() >= self._cfg.start_date]
        if self._cfg.end_date:
            timeline = [t for t in timeline if t.date() <= self._cfg.end_date]
        if not timeline:
            return result

        date_sets = {tk: set(p.df.index) for tk, p in prepped.items()}

        # ── walking state ─────────────────────────────────────────────────
        open_trades: dict[str, Trade] = {}
        pending_entries: dict[str, SignalResult] = {}
        pending_exits: set[str] = set()
        bars_walked = 0

        # Drawdown gate active here in run_all() as well as in run_prepped().
        dd_gate = _DrawdownGate(self._cfg.max_drawdown_r)

        for D in timeline:
            D_date = D.date()
            active = [tk for tk in prepped if D in date_sets[tk]]
            if not active:
                continue

            # Pre-slice market context ONCE per bar — reused across all
            # ticker engine calls on this date.
            market_t = (
                {sym: mdf.loc[:D] for sym, mdf in market_dfs.items()}
                if market_dfs else None
            )
            vix_t = vix_df.loc[:D] if vix_df is not None else None

            # Compute regime ONCE per bar — same for every ticker on this date.
            # Avoids N redundant calls to _market_regime inside engine.signal.
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

            closed_this_bar: set[str] = set()

            # ── Phase 1: pending exits fill at open (frees slots) ─────────
            for ticker in sorted(active):
                if ticker not in pending_exits or ticker not in open_trades:
                    continue
                bar = prepped[ticker].df.loc[D]
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                _close_trade(
                    open_trades[ticker],
                    exit_date=D_date,
                    exit_price=float(bar["open"]),
                    reason="engine_exit",
                    df_index=prepped[ticker].df.index,
                    exit_idx=t_idx,
                    commission_r=self._cfg.commission_r,
                )
                closed = open_trades.pop(ticker)
                result.trades.append(closed)
                self._record_close(closed)
                dd_gate.record(closed.effective_r)
                pending_exits.discard(ticker)
                closed_this_bar.add(ticker)

            # ── Drawdown circuit breaker ────────────────────────────────
            dd_gate.reset_for_new_bar()
            if dd_gate.blocked:
                # Discard queued entries — they'd otherwise sit waiting forever
                for ticker in list(pending_entries.keys()):
                    result.capped_signals.append(CappedSignal(
                        date=D_date,
                        ticker=ticker,
                        signal=pending_entries.pop(ticker),
                    ))

            # ── Phase 2: pending entries fill at open — score-ranked ──────
            # Highest-scoring signal wins the slot when cap is contested.
            # Falls back to score=0.0 (alphabetical) when scorer not used.
            sorted_entries = sorted(
                [tk for tk in pending_entries if D in date_sets[tk]],
                key=lambda tk: getattr(pending_entries[tk], "score", 0.0),
                reverse=True,  # highest score first
            )
            for ticker in sorted_entries:
                if ticker in closed_this_bar:
                    # Pathological — shouldn't happen (pending_entries only
                    # set when flat) but guard for safety.
                    pending_entries.pop(ticker, None)
                    continue
                if len(open_trades) >= self._cfg.max_concurrent:
                    result.capped_signals.append(CappedSignal(
                        date=D_date,
                        ticker=ticker,
                        signal=pending_entries.pop(ticker),
                    ))
                    continue

                signal = pending_entries.pop(ticker)
                # Compose final multiplier: regime/behavioral (already on
                # signal) × chronic-loser penalty. Zero or negative blocks
                # the entry, mirroring the regime-zero contract.
                base_mult = float(getattr(signal, "size_mult", 1.0))
                chronic_mult = (
                    self._cfg.ticker_health.size_multiplier(ticker, D_date)
                    if self._cfg.ticker_health is not None
                    else 1.0
                )
                final_mult = base_mult * chronic_mult

                if final_mult <= 0:
                    result.capped_signals.append(CappedSignal(
                        date=D_date, ticker=ticker, signal=signal,
                    ))
                    continue

                bar = prepped[ticker].df.loc[D]
                raw_entry = float(bar["open"])
                # Phase 10.3: slippage sign-aware.
                # Long  : buy at a *worse* (higher) price → 1 + slip
                # Short : sell at a *worse* (lower) price → 1 - slip
                _is_short = (signal.direction == "short")
                slip_mult = (1.0 - self._cfg.entry_slippage_pct) if _is_short else (1.0 + self._cfg.entry_slippage_pct)
                actual_entry = raw_entry * slip_mult
                # Re-anchor target on the slipped entry so realised R on a
                # target hit equals the configured min_rr.
                adj_target = adjust_target_for_slippage(
                    actual_entry,
                    float(signal.stop_price),
                    float(signal.target_price),
                    float(getattr(signal, "min_rr", 0.0) or 0.0),
                    direction=signal.direction,
                )

                open_trades[ticker] = Trade(
                    ticker=ticker,
                    signal_type=signal.signal_type,
                    direction=signal.direction,
                    entry_date=D_date,
                    entry_price=actual_entry,
                    initial_stop=float(signal.stop_price),
                    initial_target=adj_target,
                    market_regime=signal.market_regime,
                    ticker_trend=signal.ticker_trend,
                    size_mult=final_mult,  # regime × chronic-loser
                    borrow_annual_rate=self._borrow_rate(ticker, signal.direction),
                )

            # ── Phase 3: stop/target check on held trades ─────────────────
            for ticker in list(open_trades.keys()):
                if ticker in closed_this_bar or D not in date_sets[ticker]:
                    continue
                trade = open_trades[ticker]
                bar = prepped[ticker].df.loc[D]
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                b_open = float(bar["open"])
                b_low = float(bar["low"])
                b_high = float(bar["high"])

                # Pessimistic same-bar: stop wins when both H/L touch.
                # Phase 10.3 — direction-aware hit conditions and fills.
                is_short = (trade.direction == "short")
                stop_hit = (b_high >= trade.initial_stop) if is_short else (b_low <= trade.initial_stop)
                if stop_hit:
                    fill = (apply_stop_fill_short(trade.initial_stop, b_open)
                            if is_short
                            else apply_stop_fill(trade.initial_stop, b_open))
                    _close_trade(trade, D_date, fill, "stop",
                                 prepped[ticker].df.index, t_idx,
                                 self._cfg.commission_r)
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
                                 prepped[ticker].df.index, t_idx,
                                 self._cfg.commission_r)
                    closed = open_trades.pop(ticker)
                    result.trades.append(closed)
                    self._record_close(closed)
                    dd_gate.record(closed.effective_r)
                    closed_this_bar.add(ticker)

            # ── Phase 4: engine signal evaluation at this bar's close ─────
            for ticker in active:
                if ticker in closed_this_bar:
                    continue
                bars_walked += 1
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                df_t = prepped[ticker].df.iloc[: t_idx + 1]
                held = ticker in open_trades

                # Phase 10.3 — held_short dispatch when the open trade is short.
                held_short_flag = held and open_trades[ticker].direction == "short"
                held_long_flag = held and not held_short_flag
                signal = call_engine_slice(
                    self._engine, ticker, df_t, D_date,
                    market_t, vix_t, prepped[ticker].earnings_history,
                    held_long_flag,
                    regime=regime,
                    held_short=held_short_flag,
                )

                if not signal.passed:
                    continue

                if held and signal.direction in ("exit_long", "exit_short"):
                    pending_exits.add(ticker)

                elif not held and signal.direction in ("long", "short"):
                    # Enrich with scorer BEFORE queuing so the score is
                    # available for Phase 2 ranking on the next bar.
                    if self._scorer is not None:
                        next_earn = (
                            next_earnings_from(
                                prepped[ticker].earnings_history, D_date
                            )
                            if prepped[ticker].earnings_history else None
                        )
                        try:
                            self._scorer.enrich(
                                signal=signal,
                                df=df_t,
                                regime=regime,
                                earnings_date=next_earn,
                                ticker=ticker,
                            )
                        except Exception as exc:
                            logger.debug("[%s] scorer.enrich failed: %s",
                                         ticker, exc)
                        if signal.watch_only:
                            continue  # below min_score_to_alert — skip entry
                    pending_entries[ticker] = signal

        # ── End-of-timeline: force-close still-open trades ────────────────
        if self._cfg.close_open_at_eod and open_trades:
            last_D = timeline[-1]
            for ticker, trade in list(open_trades.items()):
                tdf = prepped[ticker].df
                in_window = tdf.loc[:last_D]
                if in_window.empty:
                    continue
                last_bar = in_window.iloc[-1]
                last_date = (
                    last_bar.name.date()
                    if hasattr(last_bar.name, "date") else last_D.date()
                )
                _close_trade(
                    trade,
                    exit_date=last_date,
                    exit_price=float(last_bar["close"]),
                    reason="open_eod",
                    df_index=tdf.index,
                    exit_idx=len(in_window) - 1,
                    commission_r=self._cfg.commission_r,
                )
                closed = open_trades.pop(ticker)
                result.trades.append(closed)
                self._record_close(closed)
                dd_gate.record(closed.effective_r)

        result.bars_walked = bars_walked
        return result

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
        if self._cfg.end_date:
            timeline = [t for t in timeline if t.date() <= self._cfg.end_date]
        if not timeline:
            return result

        date_sets = {tk: set(p.df.index) for tk, p in prepped.items()}
        open_trades = {}
        pending_entries = {}
        pending_exits = set()
        bars_walked = 0

        # Drawdown gate lives in a shared helper so the same
        # behaviour applies to run_all() and run_prepped() and is testable.
        dd_gate = _DrawdownGate(self._cfg.max_drawdown_r)

        for D in timeline:
            D_date = D.date()
            active = [tk for tk in prepped if D in date_sets[tk]]
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

            # Phase 1: pending exits at open
            for ticker in sorted(active):
                if ticker not in pending_exits or ticker not in open_trades:
                    continue
                bar = prepped[ticker].df.loc[D]
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                _close_trade(open_trades[ticker], D_date, float(bar["open"]),
                             "engine_exit", prepped[ticker].df.index, t_idx,
                             self._cfg.commission_r)
                closed = open_trades.pop(ticker)
                result.trades.append(closed)
                self._record_close(closed)
                dd_gate.record(closed.effective_r)
                pending_exits.discard(ticker)
                closed_this_bar.add(ticker)

            # Phase 2: pending entries at open, score-ranked
            # Drawdown circuit breaker
            dd_gate.reset_for_new_bar()
            if dd_gate.blocked:
                # Skip entry fills but still process exits
                for ticker in sorted(
                        [tk for tk in pending_entries if D in date_sets[tk]],
                        key=lambda tk: getattr(pending_entries[tk], "score", 0.0),
                        reverse=True,
                ):
                    result.capped_signals.append(
                        CappedSignal(D_date, ticker, pending_entries.pop(ticker))
                    )
            else:
                for ticker in sorted(
                        [tk for tk in pending_entries if D in date_sets[tk]],
                        key=lambda tk: getattr(pending_entries[tk], "score", 0.0),
                        reverse=True,
                ):
                    if ticker in closed_this_bar:
                        pending_entries.pop(ticker, None)
                        continue
                    if len(open_trades) >= self._cfg.max_concurrent:
                        result.capped_signals.append(
                            CappedSignal(D_date, ticker, pending_entries.pop(ticker))
                        )
                        continue
                    signal = pending_entries.pop(ticker)
                    base_mult = float(getattr(signal, "size_mult", 1.0))
                    chronic_mult = (
                        self._cfg.ticker_health.size_multiplier(ticker, D_date)
                        if self._cfg.ticker_health is not None
                        else 1.0
                    )
                    final_mult = base_mult * chronic_mult
                    if final_mult <= 0:  # regime + chronic-loser
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
                        size_mult=final_mult,  # regime × chronic-loser
                        borrow_annual_rate=self._borrow_rate(ticker, signal.direction),
                    )

            # Phase 3: stop / target on held trades
            for ticker in list(open_trades):
                if ticker in closed_this_bar or D not in date_sets[ticker]:
                    continue
                trade = open_trades[ticker]
                bar = prepped[ticker].df.loc[D]
                t_idx = int(prepped[ticker].df.index.get_loc(D))
                b_open, b_low, b_high = float(bar["open"]), float(bar["low"]), float(bar["high"])
                is_short = (trade.direction == "short")
                stop_hit = (b_high >= trade.initial_stop) if is_short else (b_low <= trade.initial_stop)
                if stop_hit:
                    fill = (apply_stop_fill_short(trade.initial_stop, b_open)
                            if is_short
                            else apply_stop_fill(trade.initial_stop, b_open))
                    _close_trade(trade, D_date, fill, "stop",
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

            # Phase 4: engine signal at close
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
                )
                if not signal.passed:
                    continue
                if held and signal.direction in ("exit_long", "exit_short"):
                    pending_exits.add(ticker)
                elif not held and signal.direction in ("long", "short"):
                    # Enrich with scorer + apply min_score_to_alert gate
                    if self._scorer is not None:
                        next_earn = (
                            next_earnings_from(
                                prepped[ticker].earnings_history, D_date
                            )
                            if prepped[ticker].earnings_history else None
                        )
                        try:
                            self._scorer.enrich(
                                signal=signal,
                                df=df_t,
                                regime=regime,
                                earnings_date=next_earn,
                                ticker=ticker,
                            )
                        except Exception as exc:
                            logger.debug("[%s] scorer.enrich failed: %s",
                                         ticker, exc)
                        if signal.watch_only:
                            continue  # below min_score_to_alert -- skip entry
                    pending_entries[ticker] = signal

        # Force-close remaining open trades
        if self._cfg.close_open_at_eod and open_trades:
            last_D = timeline[-1]
            for ticker, trade in list(open_trades.items()):
                tdf = prepped[ticker].df
                in_win = tdf.loc[:last_D]
                if in_win.empty:
                    continue
                last_bar = in_win.iloc[-1]
                last_date = last_bar.name.date() if hasattr(last_bar.name, "date") else last_D.date()
                _close_trade(trade, last_date, float(last_bar["close"]), "open_eod",
                             tdf.index, len(in_win) - 1, self._cfg.commission_r)
                closed = open_trades.pop(ticker)
                result.trades.append(closed)
                self._record_close(closed)
                dd_gate.record(closed.effective_r)

        result.bars_walked = bars_walked
        return result
