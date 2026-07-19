"""Robustness stress readouts over a trade list — display-only post-processing.

Answers one question about an aggregate result: is it carried by a single
stretch of history? (The regime-flip-exit lesson: +32R in aggregate, refuted by
10/26 yearly windows.) Pure functions over the trade list the backtester already
produced — nothing here changes trade generation, so any gate that prints these
stays byte-identical.
"""

from __future__ import annotations

from backtest.equity_curve import build_curve

# Era folds shared with the paired studies (liquidity / chronic / throttle).
DEFAULT_ERAS: tuple[tuple[int, int], ...] = ((2000, 2010), (2011, 2017), (2018, 2026))


def _year(trade) -> int:
    return trade.entry_date.year


def era_rows(trades, eras=DEFAULT_ERAS) -> list[tuple[str, int, float, float]]:
    """Per-era (label, trades, total_r, sharpe) rows; empty eras skipped."""
    rows = []
    for lo, hi in eras:
        sub = [t for t in trades if lo <= _year(t) <= hi]
        if not sub:
            continue
        ec = build_curve(sub)
        rows.append((f"{lo}-{hi}", len(sub), ec.total_r, ec.sharpe))
    return rows


def drop_best(trades, groups: list[tuple[str, list]]) -> tuple[str, object] | None:
    """Remove the highest-contributing group and re-measure the remainder.

    ``groups`` is [(label, subset_trades)]; the best group is the one whose own
    curve carries the most total_r. Returns (best_label, curve_without_it), or
    None when fewer than two non-empty groups exist (nothing to drop)."""
    scored = [(label, build_curve(sub).total_r, sub) for label, sub in groups if sub]
    if len(scored) < 2:
        return None
    best_label, _, best_sub = max(scored, key=lambda x: x[1])
    best_ids = {id(t) for t in best_sub}
    rest = [t for t in trades if id(t) not in best_ids]
    return best_label, build_curve(rest)


def drop_best_year(trades):
    """(best_year_label, curve_without_it) — the leave-one-year-out worst case."""
    years = sorted({_year(t) for t in trades})
    groups = [(str(y), [t for t in trades if _year(t) == y]) for y in years]
    return drop_best(trades, groups)


def drop_best_era(trades, eras=DEFAULT_ERAS):
    """(best_era_label, curve_without_it) — the leave-one-era-out worst case."""
    groups = [(f"{lo}-{hi}", [t for t in trades if lo <= _year(t) <= hi])
              for lo, hi in eras]
    return drop_best(trades, groups)


def print_stress(trades, *, label: str = "leg") -> None:
    """The standing robustness block: era table + leave-one-out worst cases.

    Display-only. A sign flip (or a Sharpe collapse) on drop-best-era means the
    aggregate is carried by one stretch — treat the headline accordingly.
    """
    base = build_curve(trades)
    print(f"\n  ROBUSTNESS — {label} (display-only; gate numbers unchanged)")
    print(f"  {'era':<12} {'trades':>7} {'totalR':>9} {'Sharpe':>7}")
    for lbl, n, tr, sr in era_rows(trades):
        print(f"  {lbl:<12} {n:>7} {tr:>+9.2f} {sr:>7.2f}")
    for name, result in (("drop best year", drop_best_year(trades)),
                         ("drop best era ", drop_best_era(trades))):
        if result is None:
            continue
        lbl, ec = result
        print(f"  {name} ({lbl}): {base.total_r:+.2f} → {ec.total_r:+.2f}R · "
              f"Sharpe {base.sharpe:.2f} → {ec.sharpe:.2f}")
    print("  (sign flip / Sharpe collapse on drop-best-era = aggregate carried "
          "by one stretch)")
