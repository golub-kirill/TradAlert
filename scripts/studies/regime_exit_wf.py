"""Walk-forward / rolling-window validation of the bear_only regime-flip exit.

bear_only is a fixed rule, not a tuned parameter, so the honest robustness test is
not IS re-optimization but temporal consistency: run baseline vs bear_only on one
shared universe, bucket the trades into consecutive 1-year OOS windows, and check
whether bear_only holds the edge across sub-periods (not just in full-period
aggregate, which a single lucky regime could carry).

Usage:
    python scripts/studies/regime_exit_wf.py [--snapshot data/snapshot_2026-06-10]
"""

from __future__ import annotations

import argparse
import copy
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from backtest.stats import compute_stats  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402


def _run(uni, base_cfg, settings, exits_over: dict):
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("signals", {}).setdefault("exits", {})
    cfg["signals"]["exits"].update(exits_over)
    exec_cfg = base_cfg.get("execution", {})
    pcfg = PortfolioConfig(
        max_open_risk=5.0,
        earnings_aware=True,
        entry_slippage_pct=exec_cfg.get("entry_slippage_pct", 0.002),
        commission_r=exec_cfg.get("commission_r", 0.005),
        close_open_at_eod=True,
        max_hold_days=int(exec_cfg.get("max_hold_days", 25)),
        max_hold_mode=str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    )
    bt = PortfolioBacktester(FilterEngine.from_dict(cfg), pcfg)
    result = bt.run_prepped(
        uni.prepped, uni.skipped, uni.market_dfs, uni.vix_df,
        macro_series=uni.macro_series, behavioral_data=uni.behavioral_data,
        spy_df=uni.spy_df, settings=settings,
    )
    return result.trades


def _by_year(trades):
    out = defaultdict(list)
    for t in trades:
        ed = getattr(t, "exit_date", None)
        if ed is not None:
            out[ed.year].append(t)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    args = ap.parse_args()
    snap = _ROOT / args.snapshot

    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", [])) if isinstance(t, str)]

    print(f"  Snapshot: {snap}", flush=True)
    uni = load_universe(
        tickers, ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True, cache_dir=snap / "prices",
        earnings_dir=snap / "earnings_history", macro_dir=snap / "macro",
        behavioral_dir=snap / "behavioral", start_date=date(2000, 1, 1),
    )
    print(f"  {uni.summary()}", flush=True)

    base = _run(uni, base_cfg, settings, {})
    print("  baseline leg done", flush=True)
    bear = _run(uni, base_cfg, settings, {"regime_flip_bear_only": True})
    print("  bear_only leg done", flush=True)

    yb, yr = _by_year(base), _by_year(bear)
    years = sorted(set(yb) | set(yr))

    print()
    print("  WALK-FORWARD — consecutive 1-year OOS windows (bear_only vs baseline)")
    print("  " + "─" * 74)
    print(f"  {'year':>6} {'base R':>9} {'bear R':>9} {'ΔR':>8} "
          f"{'base Shp':>9} {'bear Shp':>9} {'ΔShp':>7}")
    wins_r = wins_s = n = 0
    for y in years:
        bt_, br_ = yb.get(y, []), yr.get(y, [])
        if not bt_ and not br_:
            continue
        n += 1
        ecb, ecr = build_curve(bt_), build_curve(br_)
        dR = ecr.total_r - ecb.total_r
        dS = ecr.sharpe - ecb.sharpe
        wins_r += dR > 0
        wins_s += dS > 0
        flag = "✓" if dR > 0 else ("·" if abs(dR) < 1e-9 else "✗")
        print(f"  {y:>6} {ecb.total_r:>+9.2f} {ecr.total_r:>+9.2f} {dR:>+8.2f} "
              f"{ecb.sharpe:>9.2f} {ecr.sharpe:>9.2f} {dS:>+7.2f}  {flag}")

    ecb, ecr = build_curve(base), build_curve(bear)
    stb, str_ = compute_stats(base), compute_stats(bear)
    print("  " + "─" * 74)
    print(f"  bear_only wins totalR in {wins_r}/{n} yearly windows · "
          f"Sharpe in {wins_s}/{n}")
    print(f"  FULL PERIOD  base {ecb.total_r:+.2f}R Sharpe {ecb.sharpe:.2f} "
          f"maxDD {ecb.max_dd:.2f} ({stb.trades_count}t)  →  "
          f"bear {ecr.total_r:+.2f}R Sharpe {ecr.sharpe:.2f} "
          f"maxDD {ecr.max_dd:.2f} ({str_.trades_count}t)")


if __name__ == "__main__":
    main()
