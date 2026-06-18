"""
Bar-replay backtester.

Pipeline per ticker
───────────────────
    1. Load cached OHLCV + attach indicators (mirrors main._attach_indicators).
    2. From the first bar with all indicators warm, walk one bar at a time.
       State machine: flat | in_position. Pending fills deferred one bar.
    3. Record every closed trade. Trades still open at end-of-data are
       force-closed at the last bar's close with reason 'open_eod'
       (configurable via BacktestConfig.close_open_at_eod).

Per-bar logic
─────────────
    if pending_entry  : open trade at this bar's OPEN.
    if pending_exit   : close trade at this bar's OPEN (engine-exit fill).

    if in_position:
        check stop / target against this bar's LOW / HIGH.
        pessimistic same-bar resolution: if both touched, stop wins.
        if neither touched: call engine.signal(held_long=True). If an
        exit fires, set pending_exit for next bar.

    if flat:
        call engine.signal(held_long=False). If an entry fires, set
        pending_entry for next bar.

Look-ahead prevention
─────────────────────
    Every engine.signal call gets:
        - df            sliced to df.iloc[:T+1]
        - market_dfs    sliced to df.index[T] per symbol
        - vix_df        sliced to df.index[T]
        - earnings_date computed from full history as of df.index[T].date()
    engine._today is rewritten to df.index[T].date() inside a try/finally
    so stop_dates and earnings buffer apply as of bar T.

Same-bar stop+target
────────────────────
    yfinance gives OHLC only — no intraday sequence. Pessimistic: assume
    stop hit first. This is the conservative choice; reported edge is a
    lower bound on actual edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from backtest.trade import Trade
from core.exits import breakeven_stop_level, max_hold_exit_due, trailing_stop_level
from core.filter_engine import FilterEngine, SignalResult
from core.indicators.indicators import attach_indicators
from core.ticker_health import TickerHealth
from core.ticker_store import TickerStore, next_earnings_from
from exceptions import InsufficientDataError

logger = logging.getLogger(__name__)


# ── stop-fill helper ─────────────────────────────────────────────────────────

def apply_stop_fill(initial_stop: float, bar_open: float) -> float:
    """Realistic stop fill price for a long trade.

    Gap-through model: if the bar opened *below* the stop (overnight news,
    gap-down), the stop-market order executes at ``bar_open`` — potentially
    much worse than ``initial_stop``. If triggered intraday (``bar_open >=
    stop > bar_low``), executes at ``initial_stop``. This is the mechanism by
    which real stops produce losses worse than −1 R.

    Args:
        initial_stop: Stop level set at signal time. Never changes.
        bar_open:     Opening price of the bar on which the stop triggered.

    Returns:
        The fill price a stop-market order would have received.
    """
    return min(initial_stop, bar_open)


def apply_target_fill(initial_target: float, bar_open: float) -> float:
    """Realistic target fill price for a long trade. Symmetric to ``apply_stop_fill``.

    Gap-through model: if the bar opened *above* the target (overnight news,
    gap-up), the target-as-limit order executes at ``bar_open`` — better
    than ``initial_target``. If triggered intraday (``bar_open <= target
    <= bar_high``), executes at ``initial_target``. Symmetric to the loss-side
    gap model so the headline R-multiple isn't biased by direction.

    Args:
        initial_target: Target level set at signal time. Never changes.
        bar_open:       Opening price of the bar on which the target triggered.

    Returns:
        The fill price a limit-sell-at-target order would have received.
    """
    return max(initial_target, bar_open)


def apply_stop_fill_short(initial_stop: float, bar_open: float) -> float:
    """Realistic stop fill price for a SHORT trade. Mirror of ``apply_stop_fill``.

    Gap-through model: for a short, the stop sits *above* entry. If the
    bar opens *above* the stop (overnight news, gap-up), the buy-to-cover
    stop-market order executes at ``bar_open`` — worse than the configured
    stop. If triggered intraday (``bar_open <= stop < bar_high``), executes
    at the stop level.

    Symmetric to ``apply_stop_fill`` so headline R-multiple isn't biased
    against the short side.
    """
    return max(initial_stop, bar_open)


def apply_target_fill_short(initial_target: float, bar_open: float) -> float:
    """Realistic target fill price for a SHORT trade. Mirror of ``apply_target_fill``.

    For a short, the target sits *below* entry. If the bar opens *below*
    the target (gap-down), the buy-to-cover limit fills at ``bar_open`` —
    better than the configured target. Intraday touches fill at the
    target level.
    """
    return min(initial_target, bar_open)


def adjust_target_for_slippage(
        entry_price: float,
        initial_stop: float,
        configured_target: float,
        min_rr: float,
        direction: str = "long",
) -> float:
    """Re-anchor the target to the slipped entry so realised R matches configured.

    Long side: ``FilterEngine`` sets
    ``target_price = close + (close - stop) * min_rr`` from the *pre-slippage*
    close, but the backtester fills at ``close * (1 + entry_slippage_pct)`` and
    ``Trade.compute_r`` derives ``risk_per_share`` from the slipped entry — so a
    pre-slippage-target hit reports r below configured min_rr. Short side
    mirrors with opposite sign (stop above entry, target below).

    The ``direction`` parameter selects the sign (default ``"long"``).

    Returns ``configured_target`` unchanged when ``min_rr <= 0`` (exit
    signals) or when ``real_risk`` is non-positive (degenerate trade —
    backtester will short-circuit on negative risk_per_share anyway).
    """
    if min_rr <= 0:
        return configured_target
    sign = -1 if direction == "short" else 1
    real_risk = sign * (entry_price - initial_stop)
    if real_risk <= 0:
        return configured_target
    return entry_price + sign * real_risk * min_rr


# ── config + result types ────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """
    Configuration for one backtest run.

    Attributes
    ----------
    start_date           : Earliest entry date. Bars before are warmup only.
                           None → first bar after MA200 is warm.
    end_date             : Latest entry date. None → end of data.
    earnings_aware       : When True, reconstruct historical earnings via
                           earnings_history and apply the buffer. When False,
                           earnings_date is always None during replay
                           (faster, but inflates results — flag the bias).
    close_open_at_eod    : When True, trades still open at end-of-data are
                           force-closed at the last bar's close with reason
                           'open_eod'. When False, they are discarded (not
                           included in stats).
    """
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    earnings_aware: bool = True
    close_open_at_eod: bool = True
    # Chronic-loser soft-penalty. None (default) → off, baseline behavior exact.
    # Pass an instance (typically
    # ``TickerHealth.from_config(cfg["chronic_loser_penalty"])``) to apply the
    # sliding-scale per-ticker size penalty.
    ticker_health: Optional["TickerHealth"] = None
    # Time-based max-hold exit (swing-horizon enforcement). None → OFF (baseline
    # bit-identical). Mirrors PortfolioConfig.max_hold_days: a still-open trade
    # closes at the bar CLOSE once held this many bars.
    #   mode "hard"          → always exit at the cap.
    #   mode "if_not_profit" → exit at the cap only when not in profit.
    max_hold_days: Optional[int] = None
    max_hold_mode: str = "hard"
    # ATR trailing stop. None → OFF (baseline bit-identical). Mirrors
    # PortfolioConfig; see core.exits.trailing_stop_level. R stays off the
    # initial stop — the trail changes only the exit price/reason.
    trail_atr_mult: Optional[float] = None
    trail_activate_r: Optional[float] = None
    breakeven_trigger_r: Optional[float] = None
    breakeven_buffer_atr: Optional[float] = None


@dataclass
class BacktestResult:
    """
    Output of BarReplayBacktester.run() for one ticker.

    Attributes
    ----------
    ticker         : Symbol.
    trades         : Closed trades, ordered by entry_date ascending.
    bars_walked    : Number of bars actually replayed.
    skipped_reason : Non-empty when the ticker was skipped before replay
                     (insufficient data, cache load failure, etc.).
    """
    ticker: str
    trades: list[Trade] = field(default_factory=list)
    bars_walked: int = 0
    skipped_reason: str = ""


# ── backtester ───────────────────────────────────────────────────────────────

class BarReplayBacktester:
    """
    Walks bars forward in time, calls FilterEngine per bar, records trades.

    Stateless across tickers — one instance can replay the entire watchlist
    sequentially. The shared FilterEngine instance is mutated (engine._today)
    on every call inside a try/finally; do not use this class from multiple
    threads on the same engine.

    Parameters
    ----------
    engine : Constructed FilterEngine. Its _today attribute is rewritten on
             every signal call and restored in a finally clause.
    cfg    : BacktestConfig.
    store  : Optional TickerStore. Used for OHLCV load and earnings history.
    """

    def __init__(
            self,
            engine: FilterEngine,
            cfg: BacktestConfig,
            store: TickerStore | None = None,
    ):
        self._engine = engine
        self._cfg = cfg
        self._store = store

    # ── public API ────────────────────────────────────────────────────────

    def run(
            self,
            ticker: str,
            market_dfs: dict[str, pd.DataFrame] | None = None,
            vix_df: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """
        Replay one ticker end-to-end.

        Steps
        -----
            1. Load cached OHLCV + attach indicators.
            2. Drop leading bars where any indicator is still NaN.
            3. Verify ma_slow + 2 bars remain (need MA200 ready + a prior bar).
            4. Resolve replay window from cfg.start_date / cfg.end_date.
            5. (Optional) fetch historical earnings dates.
            6. Walk bars via _walk().

        Failures at steps 1–3 produce a BacktestResult with skipped_reason
        set and an empty trades list — never raise from this method.
        """
        result = BacktestResult(ticker=ticker)

        try:
            df = (
                self._store.load_ohlcv(ticker)
                if self._store is not None
                else _load_ohlcv_fallback(ticker)
            )
            df = _attach_indicators(df)
        except Exception as exc:
            result.skipped_reason = f"load/indicators failed: {exc}"
            logger.warning("[%s] %s", ticker, result.skipped_reason)
            return result

        ma_slow = self._engine.cfg.trend.ma_slow

        # Drop leading rows where any indicator column is NaN — engine.signal
        # cannot evaluate gates on bars with NaN macd_hist / rsi.
        indicator_cols = ["atr", "rsi", "macd", "macd_signal", "macd_hist"]
        ready_mask = df[indicator_cols].notna().all(axis=1)
        if not ready_mask.any():
            result.skipped_reason = "indicators never warm — too few bars"
            return result
        df = df.loc[ready_mask.idxmax():]

        if len(df) < ma_slow + 2:
            result.skipped_reason = (
                f"only {len(df)} bars after warmup, need {ma_slow + 2}"
            )
            return result

        start = self._cfg.start_date or df.index[ma_slow].date()
        end = self._cfg.end_date or df.index[-1].date()

        earnings_history: list[date] = []
        if self._cfg.earnings_aware and self._store is not None:
            try:
                earnings_history = self._store.get_earnings_history(ticker)
            except Exception as exc:
                logger.warning("[%s] earnings history failed (continuing) — %s",
                               ticker, exc)

        trades, bars_walked = self._walk(
            df, ticker, market_dfs, vix_df, earnings_history, start, end,
        )
        result.trades = trades
        result.bars_walked = bars_walked
        return result

    # ── inner loop ────────────────────────────────────────────────────────

    def _walk(
            self,
            df: pd.DataFrame,
            ticker: str,
            market_dfs: dict[str, pd.DataFrame] | None,
            vix_df: pd.DataFrame | None,
            earnings_history: list[date],
            start: date,
            end: date,
    ) -> tuple[list[Trade], int]:
        """
        Inner replay loop. Returns (trades, bars_walked).

        State machine — flat | in_position. Pending fills deferred one bar.
        """
        trades: list[Trade] = []
        open_trade: Trade | None = None
        pending_entry: SignalResult | None = None
        pending_exit: bool = False

        start_idx = int(df.index.searchsorted(pd.Timestamp(start), side="left"))
        end_idx = int(df.index.searchsorted(pd.Timestamp(end), side="right")) - 1

        if start_idx > end_idx or start_idx >= len(df):
            return trades, 0

        bars_walked = 0

        for T in range(start_idx, end_idx + 1):
            bars_walked += 1
            bar = df.iloc[T]
            today = df.index[T].date()

            # ── 1. Execute pending fills at this bar's open ──────────────
            if pending_entry is not None and open_trade is None:
                # Compose final size multiplier from regime/behavioral
                # (already on the SignalResult) × chronic-loser penalty
                # (if enabled). Zero or negative → skip the entry, same
                # contract as the regime-zero short-circuit.
                base_mult = float(getattr(pending_entry, "size_mult", 1.0))
                chronic_mult = (
                    self._cfg.ticker_health.size_multiplier(ticker, today)
                    if self._cfg.ticker_health is not None
                    else 1.0
                )
                final_mult = base_mult * chronic_mult

                if final_mult <= 0:
                    pending_entry = None
                else:
                    # Trade direction follows the queued signal.
                    # BarReplayBacktester has no entry_slippage in its
                    # BacktestConfig (that lives only on PortfolioConfig),
                    # so bar_open is the realised entry for both sides.
                    open_trade = Trade(
                        ticker=ticker,
                        signal_type=pending_entry.signal_type,
                        direction=pending_entry.direction,
                        entry_date=today,
                        entry_price=float(bar["open"]),
                        initial_stop=float(pending_entry.stop_price),
                        initial_target=float(pending_entry.target_price),
                        market_regime=pending_entry.market_regime,
                        ticker_trend=pending_entry.ticker_trend,
                        size_mult=final_mult,
                    )
                    pending_entry = None

            if pending_exit and open_trade is not None:
                _close_trade(
                    open_trade,
                    exit_date=today,
                    exit_price=float(bar["open"]),
                    reason="engine_exit",
                    df_index=df.index,
                    exit_idx=T,
                )
                trades.append(open_trade)
                self._record_close(ticker, open_trade)
                open_trade = None
                pending_exit = False
                continue  # no new entry the same bar an exit filled

            # ── 2. In-position: stop / target check on bar T's H/L ───────
            if open_trade is not None:
                stop = open_trade.initial_stop
                target = open_trade.initial_target
                is_short = (open_trade.direction == "short")
                b_open = float(bar["open"])
                b_low = float(bar["low"])
                b_high = float(bar["high"])
                open_trade.update_excursion(b_high, b_low)  # exit-quality instrumentation

                # Same-bar pessimistic: if BOTH touched, stop wins.
                # Long  : stop hit when bar_low <= stop (price falling)
                # Short : stop hit when bar_high >= stop (price rallying)
                # Effective stop = trailing/dynamic stop once set, else initial. Set
                # at the PREVIOUS bar's end (look-ahead-free); R stays off the
                # initial stop.
                eff_stop = open_trade.current_stop if open_trade.current_stop is not None else stop
                stop_reason = ((open_trade.current_stop_reason or "stop")
                               if open_trade.current_stop is not None and open_trade.current_stop != stop
                               else "stop")
                stop_hit = (b_high >= eff_stop) if is_short else (b_low <= eff_stop)
                if stop_hit:
                    fill = (apply_stop_fill_short(eff_stop, b_open)
                            if is_short
                            else apply_stop_fill(eff_stop, b_open))
                    _close_trade(open_trade, today, fill, stop_reason,
                                 df.index, T)
                    trades.append(open_trade)
                    self._record_close(ticker, open_trade)
                    open_trade = None
                    continue

                # Long  : target hit when bar_high >= target (price rallying)
                # Short : target hit when bar_low <= target (price falling)
                target_hit = (b_low <= target) if is_short else (b_high >= target)
                if target_hit:
                    fill = (apply_target_fill_short(target, b_open)
                            if is_short
                            else apply_target_fill(target, b_open))
                    _close_trade(open_trade, today, fill, "target",
                                 df.index, T)
                    trades.append(open_trade)
                    self._record_close(ticker, open_trade)
                    open_trade = None
                    continue

                # Time-based max-hold exit (opt-in). Neither stop nor target
                # hit this bar; close at this bar's CLOSE once the trade has
                # been held max_hold_days bars. Off when None → baseline same.
                if self._cfg.max_hold_days is not None:
                    entry_pos = int(df.index.searchsorted(
                        pd.Timestamp(open_trade.entry_date)))
                    b_close = float(bar["close"])
                    if max_hold_exit_due(
                            bars_held=T - entry_pos, current_close=b_close,
                            entry_price=open_trade.entry_price,
                            side=("short" if is_short else "long"),
                            max_hold_days=self._cfg.max_hold_days,
                            mode=self._cfg.max_hold_mode):
                        _close_trade(open_trade, today, b_close, "time_stop",
                                     df.index, T)
                        trades.append(open_trade)
                        self._record_close(ticker, open_trade)
                        open_trade = None
                        continue

                # Neither touched — check engine exit signal at this close.
                # Dispatch by trade direction: held_short signals the engine
                # to evaluate the cover-short logic in _signal_exit_short.
                signal = self._call_engine(
                    ticker, df, T, today, market_dfs, vix_df,
                    earnings_history,
                    held_long=(not is_short),
                    held_short=is_short,
                )
                if signal.passed and signal.direction in ("exit_long", "exit_short"):
                    pending_exit = True
                # Still open → ratchet the dynamic stop (trail/breakeven) for the NEXT
                # bar (the level checked above was the previous bar's — look-ahead-free).
                if (self._cfg.trail_atr_mult or self._cfg.breakeven_trigger_r is not None) \
                        and open_trade is not None:
                    _atr = float(bar["atr"]) if pd.notna(bar["atr"]) else None
                    _apply_dynamic_stop(
                        open_trade, _atr, is_short,
                        trail_atr_mult=self._cfg.trail_atr_mult,
                        trail_activate_r=self._cfg.trail_activate_r,
                        breakeven_trigger_r=self._cfg.breakeven_trigger_r,
                        breakeven_buffer_atr=self._cfg.breakeven_buffer_atr,
                    )
                continue

            # ── 3. Flat: look for entry signal ───────────────────────────
            signal = self._call_engine(
                ticker, df, T, today, market_dfs, vix_df,
                earnings_history, held_long=False,
            )
            if signal.passed and signal.direction in ("long", "short"):
                pending_entry = signal

        # ── End-of-data: close any still-open trade ───────────────────────
        if open_trade is not None and self._cfg.close_open_at_eod:
            last_bar = df.iloc[end_idx]
            last_date = (
                last_bar.name.date()
                if hasattr(last_bar.name, "date") else end
            )
            _close_trade(
                open_trade,
                exit_date=last_date,
                exit_price=float(last_bar["close"]),
                reason="open_eod",
                df_index=df.index,
                exit_idx=end_idx,
            )
            trades.append(open_trade)
            self._record_close(ticker, open_trade)

        return trades, bars_walked

    # ── ledger helper ─────────────────────────────────────────────────────

    def _record_close(self, ticker: str, trade: Trade) -> None:
        """Forward a closed trade to the chronic-loser tracker (no-op when
        ``cfg.ticker_health`` is None).
        """
        if self._cfg.ticker_health is None or trade.exit_date is None:
            return
        try:
            self._cfg.ticker_health.record_trade(
                ticker, trade.exit_date, float(trade.r_multiple),
            )
        except Exception as exc:  # noqa: BLE001 - tracker must never break a backtest
            logger.warning(
                "[%s] ticker_health.record_trade failed (continuing): %s",
                ticker, exc,
            )

    # ── engine wrapper ────────────────────────────────────────────────────

    def _call_engine(
            self,
            ticker: str,
            df: pd.DataFrame,
            T: int,
            today: date,
            market_dfs: dict[str, pd.DataFrame] | None,
            vix_df: pd.DataFrame | None,
            earnings_history: list[date],
            held_long: bool,
            held_short: bool = False,
    ) -> SignalResult:
        """
        Make one point-in-time engine.signal call.

        Slices every market-context DataFrame to bar T inclusive, sets the
        engine's `_today` to df.index[T].date(), computes next-earnings as
        of today from cached history, calls signal(), and restores _today.

        Engine exceptions (typically InsufficientDataError for very young
        tickers) are converted to a blocked SignalResult so the walk can
        continue.
        """
        bar_ts = df.index[T]
        df_t = df.iloc[: T + 1]
        market_t = (
            {sym: mdf.loc[:bar_ts] for sym, mdf in market_dfs.items()}
            if market_dfs else None
        )
        vix_t = vix_df.loc[:bar_ts] if vix_df is not None else None
        next_earn = (
            next_earnings_from(earnings_history, today)
            if earnings_history else None
        )

        # Mutate then restore — engine has no per-call today parameter.
        saved_today = self._engine._today
        self._engine._today = today
        try:
            return self._engine.signal(
                ticker, df_t,
                market_dfs=market_t,
                vix_df=vix_t,
                earnings_date=next_earn,
                held_long=held_long,
                held_short=held_short,
            )
        except InsufficientDataError as exc:
            logger.debug("[%s] engine.signal insufficient data at %s: %s",
                         ticker, today, exc)
            return SignalResult(passed=False, reason=f"insufficient data: {exc}")
        except Exception as exc:
            logger.warning("[%s] engine.signal raised at %s: %s",
                           ticker, today, exc)
            return SignalResult(passed=False, reason=f"engine raised: {exc}")
        finally:
            self._engine._today = saved_today


# ── module-level helpers ──────────────────────────────────────────────────────

def _apply_dynamic_stop(
        trade: Trade,
        atr: float | None,
        is_short: bool,
        *,
        trail_atr_mult,
        trail_activate_r,
        breakeven_trigger_r,
        breakeven_buffer_atr,
) -> None:
    """Ratchet ``trade.current_stop`` via the trailing and/or breakeven rules (in the
    trade's favor only) and tag ``current_stop_reason``.

    Call at the END of each held bar; the resulting level is checked on the NEXT bar
    (look-ahead-free). Shared by both backtesters so single == portfolio.
    """
    side = "short" if is_short else "long"
    mfe = trade.current_mfe_r()
    if trail_atr_mult:
        new = trailing_stop_level(
            side=side, highest_high=trade.highest_high, lowest_low=trade.lowest_low,
            atr=atr, trail_atr_mult=trail_atr_mult, prev_stop=trade.current_stop,
            initial_stop=trade.initial_stop, mfe_r=mfe, activate_r=trail_activate_r)
        if new is not None and new != trade.current_stop:
            trade.current_stop = new
            if new != trade.initial_stop:
                trade.current_stop_reason = "trail_stop"
    if breakeven_trigger_r is not None:
        new = breakeven_stop_level(
            side=side, entry_price=trade.entry_price, atr=atr,
            breakeven_trigger_r=breakeven_trigger_r, breakeven_buffer_atr=breakeven_buffer_atr,
            prev_stop=trade.current_stop, initial_stop=trade.initial_stop, mfe_r=mfe)
        if new is not None and new != trade.current_stop:
            trade.current_stop = new
            if new != trade.initial_stop:
                trade.current_stop_reason = "breakeven_stop"


def _close_trade(
        trade: Trade,
        exit_date: date,
        exit_price: float,
        reason: str,
        df_index: pd.DatetimeIndex,
        exit_idx: int,
        commission_r: float = 0.0,
) -> None:
    """
    Populate exit fields on `trade` and compute r_multiple.

    bars_held is computed by searching back for the entry_date in df_index,
    which avoids carrying an entry-index field on Trade.

    commission_r is subtracted from the r_multiple after computation —
    a flat round-trip cost in R units (e.g. 0.005 = half a basis-point R drag).
    """
    trade.exit_date = exit_date
    trade.exit_price = float(exit_price)
    trade.exit_reason = reason  # type: ignore[assignment]

    try:
        entry_pos = int(df_index.searchsorted(pd.Timestamp(trade.entry_date)))
        trade.bars_held = max(0, exit_idx - entry_pos)
    except (TypeError, ValueError, KeyError) as exc:
        logger.debug("bars_held computation failed for %s (defaulting to 0): %s",
                     trade.ticker, exc)
        trade.bars_held = 0

    trade.r_multiple = trade.compute_r() - commission_r
    trade.compute_excursion_r()  # finalize MFE/MAE (after r_multiple is set)


def _load_ohlcv_fallback(ticker: str) -> pd.DataFrame:
    """
    Load OHLCV from the legacy parquet cache when no TickerStore is supplied.

    Kept for backward compatibility in tests that construct
    BarReplayBacktester without a store. Raises FileNotFoundError when the
    parquet file is absent — same contract as TickerStore.load_ohlcv().
    """
    from persistence.cache import load as _cache_load  # local import avoids circular
    return _cache_load(ticker)


def _attach_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a copy of df with all standard indicator columns attached.

    Delegates to ``core.indicators.indicators.attach_indicators`` — the
    single canonical implementation shared with the live pipeline (avoids a
    divergent copy).
    """
    return attach_indicators(df)


def call_engine_slice(
        engine,
        ticker,
        df_t,
        today,
        market_t,
        vix_t,
        earnings_history,
        held_long,
        regime=None,
        held_short: bool = False,
):
    """Module-level engine.signal wrapper used by PortfolioBacktester.

    Slices every context frame to bar T, sets engine._today, calls
    engine.signal(), and restores _today in a finally clause.
    Never raises: exceptions are caught and returned as blocked results.

    If a pre-computed regime is provided (enriched with macro/behavioral),
    it is passed through to engine.signal().
    """
    from backtest.earnings_history import next_earnings_from as _nef
    from core.filter_engine import SignalResult

    next_earn = _nef(earnings_history, today) if earnings_history else None
    saved = engine._today
    engine._today = today
    try:
        return engine.signal(
            ticker, df_t,
            market_dfs=market_t,
            vix_df=vix_t,
            earnings_date=next_earn,
            held_long=held_long,
            held_short=held_short,
            regime=regime,
        )
    except InsufficientDataError as exc:
        logger.debug("[%s] engine.signal insufficient data at %s: %s", ticker, today, exc)
        return SignalResult(passed=False, reason=f"insufficient data: {exc}")
    except Exception as exc:
        logger.warning("[%s] engine.signal raised at %s: %s", ticker, today, exc)
        return SignalResult(passed=False, reason=f"engine raised: {exc}")
    finally:
        engine._today = saved
