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
from core.filter_engine import FilterEngine, SignalResult
from core.indicators.indicators import attach_indicators
from core.scoring import SignalScorer
from core.ticker_store import TickerStore, next_earnings_from

logger = logging.getLogger(__name__)


# ── stop-fill helper ─────────────────────────────────────────────────────────

def apply_stop_fill(initial_stop: float, bar_open: float) -> float:
    """Realistic stop fill price for a long trade.

    Gap-through model: if the bar opened *below* the stop (overnight news,
    gap-down), the stop-market order executes at ``bar_open`` — potentially
    much worse than ``initial_stop``. If triggered intraday (``bar_open >=
    stop > bar_low``), executes at ``initial_stop``.

    This is the primary mechanism by which real stops produce losses worse
    than −1 R. Without it every stop reports exactly −1 R regardless of how
    badly price gapped.

    Args:
        initial_stop: Stop level set at signal time. Never changes.
        bar_open:     Opening price of the bar on which the stop triggered.

    Returns:
        The fill price a stop-market order would have received.
    """
    return min(initial_stop, bar_open)


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
    scorer : Optional SignalScorer. When supplied, entry scores are computed
             point-in-time and stamped on every Trade.
    store  : Optional TickerStore. Used for OHLCV load and earnings history.
    """

    def __init__(
            self,
            engine: FilterEngine,
            cfg: BacktestConfig,
            scorer: SignalScorer | None = None,
            store: TickerStore | None = None,
    ):
        self._engine = engine
        self._cfg = cfg
        self._scorer = scorer
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

        ma_slow = self._engine._cfg["trend"]["ma_slow"]

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
                # P0-6: skip the entry if regime says zero-size; equivalent to
                # "don't trade at all" for the simple backtester.
                if getattr(pending_entry, "size_mult", 1.0) <= 0:
                    pending_entry = None
                else:
                    open_trade = Trade(
                        ticker=ticker,
                        signal_type=pending_entry.signal_type,
                        direction="long",
                        entry_date=today,
                        entry_price=float(bar["open"]),
                        initial_stop=float(pending_entry.stop_price),
                        initial_target=float(pending_entry.target_price),
                        market_regime=pending_entry.market_regime,
                        ticker_trend=pending_entry.ticker_trend,
                        size_mult=float(getattr(pending_entry, "size_mult", 1.0)),
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
                open_trade = None
                pending_exit = False
                continue  # no new entry the same bar an exit filled

            # ── 2. In-position: stop / target check on bar T's H/L ───────
            if open_trade is not None:
                stop = open_trade.initial_stop
                target = open_trade.initial_target

                # Same-bar pessimistic: if BOTH touched, stop wins.
                # apply_stop_fill models gap-through: if bar opened below the
                # stop (e.g. overnight news), fill at bar_open, not stop level.
                if float(bar["low"]) <= stop:
                    fill = apply_stop_fill(stop, float(bar["open"]))
                    _close_trade(open_trade, today, fill, "stop",
                                 df.index, T)
                    trades.append(open_trade)
                    open_trade = None
                    continue

                if float(bar["high"]) >= target:
                    _close_trade(open_trade, today, target, "target",
                                 df.index, T)
                    trades.append(open_trade)
                    open_trade = None
                    continue

                # Neither touched — check engine exit signal at this close.
                signal = self._call_engine(
                    ticker, df, T, today, market_dfs, vix_df,
                    earnings_history, held_long=True,
                )
                if signal.passed and signal.direction == "exit_long":
                    pending_exit = True
                continue

            # ── 3. Flat: look for entry signal ───────────────────────────
            signal = self._call_engine(
                ticker, df, T, today, market_dfs, vix_df,
                earnings_history, held_long=False,
            )
            if signal.passed and signal.direction == "long":
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

        return trades, bars_walked

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
            )
        except Exception as exc:
            logger.debug("[%s] engine.signal raised at %s: %s",
                         ticker, today, exc)
            return SignalResult(passed=False, reason=f"engine raised: {exc}")
        finally:
            self._engine._today = saved_today


# ── module-level helpers ──────────────────────────────────────────────────────

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
    except Exception:
        trade.bars_held = 0

    trade.r_multiple = trade.compute_r() - commission_r


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
    single canonical implementation shared with the live pipeline.
    Previously duplicated here without Bollinger Bands (BUG-03 in TODO).
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
            regime=regime,
        )
    except Exception as exc:
        logger.debug("[%s] engine.signal raised at %s: %s", ticker, today, exc)
        return SignalResult(passed=False, reason=f"engine raised: {exc}")
    finally:
        engine._today = saved
