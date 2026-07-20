"""
Single-entry counterfactual replay — "what would this entry actually have paid?"

Answers the question a raw forward return cannot: a name held blindly for N bars
is not the strategy. The strategy stops out, takes targets, times out, ratchets
to breakeven and honours the engine's exit chain. This module replays one
hypothetical entry through that exact ladder and returns an R-multiple plus
MFE/MAE in R.

It is a deliberate mirror of ``BarReplayBacktester._walk``'s held-position path
(``backtest/backtester.py``), and lives beside it so the two stay visibly in
sync. Every price, level and R computation is delegated to the backtester's own
primitives — ``apply_stop_fill`` / ``apply_target_fill`` (+ short mirrors),
``_apply_dynamic_stop``, ``_close_trade``, ``core.exits.max_hold_exit_due`` and
``Trade`` — so only the loop skeleton is local. Nothing here re-derives a
formula the engine already owns.

Bar order, which must not be reordered (see ``_walk`` lines 408-512):

    1. a deferred engine exit fills at THIS bar's open  (before excursion, so the
       fill bar's H/L never enters MFE/MAE)
    2. ``update_excursion(high, low)``
    3. effective-stop check — same-bar pessimistic, the STOP WINS over the target
    4. target check
    5. max-hold, closing at this bar's CLOSE
    6. engine exit probe at this close → defers the fill to the next bar
    7. ratchet the dynamic stop for the NEXT bar — always last

Steps 3 and 7 are the look-ahead boundary: the level checked at step 3 was set at
the END of the previous bar. Moving step 7 above step 3 would let a bar's own
high set the stop its own low then triggers, manufacturing free breakeven exits.

The engine layer is reached through an ``ExitProbe`` closure rather than a
``FilterEngine`` argument, so the bar loop has no engine knowledge and tests
inject ``lambda k: k == 3`` without constructing one.

Friction defaults to ZERO here, matching bare ``_walk``, so the equivalence test
is exact. Callers that want a realistic read pass the live ``execution.*`` values
(entry/exit slippage, ``commission_r``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from backtest.backtester import (
    _apply_dynamic_stop,
    _close_trade,
    adjust_target_for_slippage,
    apply_stop_fill,
    apply_stop_fill_short,
    apply_target_fill,
    apply_target_fill_short,
    call_engine_slice,
)
from backtest.trade import Trade
from core.exits import max_hold_exit_due

# bar index -> True when the engine's exit chain fired at that bar's close
ExitProbe = Callable[[int], bool]


@dataclass(frozen=True)
class CounterfactualResult:
    """Outcome of one replayed hypothetical entry.

    ``matured`` is False when the walk ran off the end of the data and the trade
    was force-closed at the last bar — not an outcome, and must be excluded from
    headline statistics.

    ``gapped_through`` marks ``risk_per_share <= 0`` at entry (the fill gapped
    past the stop). ``Trade.compute_r`` books those at 0R by design, so they are
    flagged rather than silently averaged in as flat trades.
    """
    ticker: str
    direction: str
    r_multiple: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    bars_held: int
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    initial_stop: float
    initial_target: float
    matured: bool
    gapped_through: bool


def replay_counterfactual(
        df: pd.DataFrame,
        *,
        signal_idx: int,
        ticker: str,
        direction: str = "long",
        # geometry — mirrors core.filter_engine's stop/target construction
        atr_mult: float = 2.5,
        min_rr: float = 2.5,
        stop_price: float | None = None,
        target_price: float | None = None,
        # exit ladder — config/filters.yaml `execution:` defaults
        max_hold_days: int | None = 25,
        max_hold_mode: str = "if_not_profit",
        breakeven_trigger_r: float | None = 1.0,
        breakeven_buffer_atr: float | None = None,
        trail_atr_mult: float | None = None,
        trail_activate_r: float | None = None,
        # friction — 0.0 mirrors bare _walk; callers pass the live values
        commission_r: float = 0.0,
        entry_slippage_pct: float = 0.0,
        exit_slippage_pct: float = 0.0,
        exit_probe: ExitProbe | None = None,
) -> CounterfactualResult | None:
    """Replay one hypothetical entry signalled on bar ``signal_idx``.

    ``signal_idx`` is bar T — the bar whose CLOSE sets the geometry. The fill is
    T+1's open, matching the backtester. Callers name the signal bar and this
    function owns the T→T+1 convention, so the off-by-one cannot be got right in
    only some code paths.

    ``df`` needs ``open``/``high``/``low``/``close`` and, unless ``stop_price``
    and ``target_price`` are both supplied, an ``atr`` column.

    Returns None when there is no fill bar yet or bar T's price/ATR is unusable
    (warmup). Otherwise always returns a closed result — force-closing at the
    last bar with ``matured=False`` when nothing triggered.
    """
    n = len(df)
    i_entry = signal_idx + 1
    if signal_idx < 0 or i_entry >= n:
        return None

    is_short = (direction == "short")
    row_t = df.iloc[signal_idx]
    close_t = float(row_t["close"])
    if not (close_t == close_t and close_t > 0):
        return None

    # Geometry off bar T's close — core.filter_engine builds stop/target the same
    # way, rounded to 4dp. Reconstructed from the frame rather than taken from
    # the journal so entry, stop, target and exits all share one price basis;
    # mixing a journaled (as-of-then) stop into a split-adjusted frame would
    # produce an instant phantom stop-out.
    if stop_price is None or target_price is None:
        atr_t = float(row_t["atr"]) if "atr" in row_t else float("nan")
        if not (atr_t == atr_t and atr_t > 0):
            return None
        dist = atr_t * atr_mult
        stop = round(close_t + dist if is_short else close_t - dist, 4)
        target = round(close_t - dist * min_rr if is_short else close_t + dist * min_rr, 4)
    else:
        stop, target = float(stop_price), float(target_price)

    entry = float(df.iloc[i_entry]["open"])
    if entry_slippage_pct:
        entry *= (1.0 - entry_slippage_pct) if is_short else (1.0 + entry_slippage_pct)
        # Re-anchor the target to the slipped entry so a target hit still pays
        # the configured R (backtest.backtester.adjust_target_for_slippage).
        target = adjust_target_for_slippage(entry, stop, target, min_rr,
                                            direction=direction)

    trade = Trade(
        ticker=ticker,
        signal_type="counterfactual",
        direction=direction,
        entry_date=df.index[i_entry].date(),
        entry_price=entry,
        initial_stop=stop,
        initial_target=target,
    )
    gapped = trade.risk_per_share <= 0

    def _slip(price: float) -> float:
        """Exit-side slippage on MARKET fills only; target limits stay exact.
        Mirrors PortfolioBacktester._exit_fill."""
        if not exit_slippage_pct:
            return float(price)
        return float(price) * ((1.0 + exit_slippage_pct) if is_short
                               else (1.0 - exit_slippage_pct))

    pending_exit = False
    matured = True
    exit_idx = n - 1

    for k in range(i_entry, n):
        bar = df.iloc[k]
        today = df.index[k].date()
        b_open, b_high = float(bar["open"]), float(bar["high"])
        b_low, b_close = float(bar["low"]), float(bar["close"])

        # 1. deferred engine exit fills at this bar's open, BEFORE excursion —
        #    _walk never reaches update_excursion on the fill bar.
        if pending_exit:
            _close_trade(trade, today, _slip(b_open), "engine_exit",
                         df.index, k, commission_r=commission_r)
            exit_idx = k
            break

        # 2. this held bar's extremes
        trade.update_excursion(b_high, b_low)

        # 3. effective stop — set at the END of bar k-1, so no look-ahead. R
        #    stays on the initial stop whatever the ratchet did.
        eff_stop = trade.current_stop if trade.current_stop is not None else trade.initial_stop
        stop_reason = ((trade.current_stop_reason or "stop")
                       if trade.current_stop is not None
                       and trade.current_stop != trade.initial_stop
                       else "stop")
        stop_hit = (b_high >= eff_stop) if is_short else (b_low <= eff_stop)
        if stop_hit:
            fill = (apply_stop_fill_short(eff_stop, b_open) if is_short
                    else apply_stop_fill(eff_stop, b_open))
            _close_trade(trade, today, _slip(fill), stop_reason,
                         df.index, k, commission_r=commission_r)
            exit_idx = k
            break

        # 4. target — reached only because the stop did not hit this bar
        target_hit = (b_low <= target) if is_short else (b_high >= target)
        if target_hit:
            fill = (apply_target_fill_short(target, b_open) if is_short
                    else apply_target_fill(target, b_open))
            _close_trade(trade, today, fill, "target",       # limit fill: no slip
                         df.index, k, commission_r=commission_r)
            exit_idx = k
            break

        # 5. max-hold at this bar's close
        if max_hold_days is not None and max_hold_exit_due(
                bars_held=k - i_entry, current_close=b_close, entry_price=entry,
                side=("short" if is_short else "long"),
                max_hold_days=max_hold_days, mode=max_hold_mode):
            _close_trade(trade, today, _slip(b_close), "time_stop",
                         df.index, k, commission_r=commission_r)
            exit_idx = k
            break

        # 6. engine exit chain at this close → fills next bar's open
        if exit_probe is not None and exit_probe(k):
            pending_exit = True

        # 7. ratchet for the NEXT bar. Must stay last, and after step 2 —
        #    _apply_dynamic_stop reads current_mfe_r(), which needs this bar's
        #    excursion. The level it sets is only CHECKED from bar k+1.
        if trail_atr_mult or breakeven_trigger_r is not None:
            _atr = float(bar["atr"]) if "atr" in bar and pd.notna(bar["atr"]) else None
            _apply_dynamic_stop(
                trade, _atr, is_short,
                trail_atr_mult=trail_atr_mult, trail_activate_r=trail_activate_r,
                breakeven_trigger_r=breakeven_trigger_r,
                breakeven_buffer_atr=breakeven_buffer_atr,
            )
    else:
        # Ran off the end of the data — force-close at the last bar and mark it
        # unmatured so it cannot be counted as an outcome.
        last = n - 1
        _close_trade(trade, df.index[last].date(), _slip(float(df.iloc[last]["close"])),
                     "open_eod", df.index, last, commission_r=commission_r)
        exit_idx = last
        matured = False

    return CounterfactualResult(
        ticker=ticker,
        direction=direction,
        r_multiple=float(trade.r_multiple),
        mfe_r=float(trade.mfe_r),
        mae_r=float(trade.mae_r),
        exit_reason=str(trade.exit_reason),
        bars_held=int(trade.bars_held),
        entry_idx=i_entry,
        exit_idx=exit_idx,
        entry_price=float(trade.entry_price),
        exit_price=float(trade.exit_price),
        initial_stop=float(trade.initial_stop),
        initial_target=float(trade.initial_target),
        matured=matured,
        gapped_through=bool(gapped),
    )


def make_engine_exit_probe(engine, ticker: str, df: pd.DataFrame,
                           market_dfs, vix_df, *, is_short: bool = False,
                           slice_cache: dict | None = None,
                           blocked: list | None = None) -> ExitProbe:
    """Build an ``ExitProbe`` that asks the real engine's exit chain at bar k.

    Owns the point-in-time slicing: ``call_engine_slice`` expects frames ALREADY
    cut to bar k (it does not slice for you), so passing whole frames would leak
    the future. Market/VIX slices are memoised by bar timestamp — scan dates
    cluster heavily, so a few hundred distinct timestamps serve tens of
    thousands of calls.

    ``earnings_history`` is deliberately ``[]``: with ``held_long``/``held_short``
    set, ``FilterEngine.signal`` branches straight to the exit chain, and the
    earnings buffer only gates the ENTRY path — so earnings context cannot change
    an exit. Passing it would cost a lookup per ticker for no behavioural
    difference.

    ``call_engine_slice`` never raises — it returns a blocked ``SignalResult``.
    That means a slicing or context bug degrades silently into "the engine never
    exits", so blocked calls are appended to ``blocked`` when supplied, for the
    caller to surface.
    """
    cache = slice_cache if slice_cache is not None else {}

    def probe(k: int) -> bool:
        bar_ts = df.index[k]
        if bar_ts not in cache:
            cache[bar_ts] = (
                ({sym: mdf.loc[:bar_ts] for sym, mdf in market_dfs.items()}
                 if market_dfs else None),
                (vix_df.loc[:bar_ts] if vix_df is not None else None),
            )
        market_t, vix_t = cache[bar_ts]
        sig = call_engine_slice(
            engine, ticker, df.iloc[: k + 1], bar_ts.date(), market_t, vix_t,
            [], held_long=(not is_short), held_short=is_short,
        )
        if blocked is not None and not sig.passed and getattr(sig, "reason", ""):
            reason = str(sig.reason)
            if reason.startswith(("engine raised:", "insufficient data:")):
                blocked.append(f"{ticker} @ {bar_ts.date()}: {reason}")
        return bool(sig.passed and sig.direction in ("exit_long", "exit_short"))

    return probe
