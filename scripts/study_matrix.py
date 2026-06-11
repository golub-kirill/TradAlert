"""
Paired study matrices on a frozen data snapshot.

One load_universe(), N config legs — every leg replays the identical in-memory
data, so cross-leg deltas are pure config effects (headline LEVELS move ±~10R
with every price-cache refresh; only same-snapshot paired comparisons count).

Built-in studies (base config = the SHIPPED defaults incl. the breakeven stop):
  b1  drawdown defense : max_drawdown_r {off,6,8,10,12} x vix_slope_block {off,on}
  b2  turnover frontier: signals.stop_loss.min_rr {1.0,1.5,2.0,2.5}
                         x max_hold_days {5,10,15,25}  (+ WR(T) ceiling table)
  b3  venue economics  : full / .TO-only / US-only universes x slippage

Custom legs (e.g. post-hoc stress cells) need no code change:
  --legs "rr15mh10s3:signals.stop_loss.min_rr=1.5,max_hold_days=10,entry_slippage_pct=0.003"

Readout per leg: trades, trades/yr, WR, E[R], total R, R/yr, Sharpe, Sortino,
maxDD, recovery days, % calendar days underwater, R/yr per window half
(split 2013-06-15), exit-reason mix. --dump-trades writes per-leg parquet
(incl. mfe_r/mae_r — these are NOT journaled to the DB) for offline analyses.

Exploratory harness: no journal, no HTML, no CSV.
"""

from __future__ import annotations

import argparse
import copy
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from backtest.equity_curve import build_curve  # noqa: E402
from backtest.loader import load_universe  # noqa: E402
from backtest.portfolio_backtester import (  # noqa: E402
    PortfolioBacktester, PortfolioConfig,
)
from backtest.stats import compute_stats  # noqa: E402
from backtest.sweep import _set_nested  # noqa: E402
from core.filter_engine import FilterEngine  # noqa: E402

HALF_SPLIT = date(2013, 6, 15)
CONTEXT_TICKERS = ("SPY", "QQQ", "^VIX")
WRT_RUNGS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]


# ── leg construction ──────────────────────────────────────────────────────────

def parse_legs_spec(spec: str) -> list[dict]:
    """``label:key=val,key=val;label2:...`` → leg dicts.

    Dotted keys mutate the engine cfg; bare keys go to PortfolioConfig;
    ``tickers=all|to|us`` selects the universe subset.
    """
    legs = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        label, _, body = part.partition(":")
        leg = {"label": label.strip(), "cfg_mut": {}, "port_mut": {},
               "tickers": "all"}
        for kv in body.split(","):
            if not kv.strip():
                continue
            key, _, raw = kv.partition("=")
            key, raw = key.strip(), raw.strip()
            if key == "tickers":
                leg["tickers"] = raw
                continue
            if raw.lower() in ("true", "false"):
                val = raw.lower() == "true"
            else:
                val = float(raw) if "." in raw or "e" in raw.lower() else int(raw)
            if "." in key:
                leg["cfg_mut"][key] = val
            else:
                leg["port_mut"][key] = val
        legs.append(leg)
    return legs


def study_legs(study: str) -> list[dict]:
    legs = []
    if study == "b1":
        for br in (None, 6.0, 8.0, 10.0, 12.0):
            for gate in (False, True):
                leg = {"label": f"br={br:g}" if br else "br=off",
                       "cfg_mut": {}, "port_mut": {}, "tickers": "all"}
                leg["label"] += ",gate=" + ("on" if gate else "off")
                if br:
                    leg["port_mut"]["max_drawdown_r"] = br
                if gate:
                    leg["cfg_mut"]["regime.vix_slope_block"] = True
                legs.append(leg)
    elif study == "b2":
        for rr in (1.0, 1.5, 2.0, 2.5):
            for mh in (5, 10, 15, 25):
                legs.append({
                    "label": f"rr={rr:g},mh={mh}",
                    "cfg_mut": {"signals.stop_loss.min_rr": rr},
                    "port_mut": {"max_hold_days": mh},
                    "tickers": "all",
                })
    elif study == "b3":
        legs = [
            {"label": "full@0.002", "cfg_mut": {}, "port_mut": {}, "tickers": "all"},
            {"label": "to_only@0.002", "cfg_mut": {}, "port_mut": {}, "tickers": "to"},
            {"label": "us_only@0.002", "cfg_mut": {}, "port_mut": {}, "tickers": "us"},
            {"label": "to_only@0.001", "cfg_mut": {},
             "port_mut": {"entry_slippage_pct": 0.001}, "tickers": "to"},
        ]
    else:
        raise SystemExit(f"unknown study {study!r}")
    return legs


def subset_tickers(prepped: dict, which: str) -> dict:
    """Filter the prepped universe to a venue subset, keeping market-context
    tickers so regime classification stays identical across legs."""
    if which == "all":
        return prepped
    if which == "to":
        keep = lambda t: t.endswith(".TO") or t in CONTEXT_TICKERS  # noqa: E731
    elif which == "us":
        keep = lambda t: not t.endswith(".TO") or t in CONTEXT_TICKERS  # noqa: E731
    else:
        raise SystemExit(f"unknown ticker subset {which!r}")
    return {t: v for t, v in prepped.items() if keep(t)}


# ── readout math (pure, unit-tested) ──────────────────────────────────────────

def underwater_pct(curve) -> float:
    """% of CALENDAR days spent below the running equity peak. The drawdown
    series is exit-date-indexed (gaps!), so reindex to a daily calendar."""
    dd = curve.drawdown
    if dd.empty:
        return 0.0
    cal = dd.reindex(pd.date_range(dd.index.min(), dd.index.max(),
                                   freq="D")).ffill()
    return float((cal > 1e-9).mean())


def half_r_per_year(trades, split: date = HALF_SPLIT) -> tuple[float, float]:
    """R/yr (effective R) in each window half — the B2 split-half hurdle."""
    out = []
    for half in (
        [t for t in trades if t.entry_date and t.entry_date < split],
        [t for t in trades if t.entry_date and t.entry_date >= split],
    ):
        if not half:
            out.append(0.0)
            continue
        lo = min(t.entry_date for t in half)
        hi = max((t.exit_date or t.entry_date) for t in half)
        years = max(0.5, (hi - lo).days / 365.25)
        out.append(sum(_eff_r(t) for t in half) / years)
    return out[0], out[1]


def wrt_table(trades, rungs=WRT_RUNGS) -> list[tuple[float, float, float]]:
    """(T, P(MFE>=T), naive implied E[R] at target T) — the max-WR-at-profit
    ceiling. Naive: reachers earn T, non-reachers keep their actual realized R
    (ignores re-entry/hold-time effects — a CEILING input, not a forecast)."""
    rows = []
    n = len(trades)
    if not n:
        return rows
    for t_rung in rungs:
        reach = [t for t in trades if (t.mfe_r or 0.0) >= t_rung]
        rest = [t for t in trades if (t.mfe_r or 0.0) < t_rung]
        p = len(reach) / n
        rest_mean = (sum(float(t.r_multiple or 0.0) for t in rest) / len(rest)
                     ) if rest else 0.0
        rows.append((t_rung, p, t_rung * p + rest_mean * (1 - p)))
    return rows


def _eff_r(t) -> float:
    return float(t.r_multiple or 0.0) * float(t.size_mult or 1.0)


def leg_row(label: str, trades) -> str:
    st = compute_stats(trades)
    ec = build_curve(trades)
    if trades:
        lo = min(t.entry_date for t in trades if t.entry_date)
        hi = max((t.exit_date or t.entry_date) for t in trades)
        years = max(1.0, (hi - lo).days / 365.25)
    else:
        years = 1.0
    h1, h2 = half_r_per_year(trades)
    rec = ec.recovery_days if ec.recovery_days is not None else -1
    mix = Counter(t.exit_reason for t in trades if t.exit_reason)
    mix_s = " ".join(f"{k}:{v}" for k, v in mix.most_common(4))
    return (f"  {label:<18} {st.trades_count:>5} {st.trades_count / years:>5.0f} "
            f"{st.win_rate:>6.1%} {st.expectancy_r:>+7.3f} {ec.total_r:>+8.2f} "
            f"{ec.total_r / years:>+6.2f} {ec.sharpe:>5.2f} {ec.sortino:>6.2f} "
            f"{ec.max_dd:>6.2f} {rec:>5d} {underwater_pct(ec):>5.1%} "
            f"{h1:>+6.2f} {h2:>+6.2f}  {mix_s}")


HEADER = (f"  {'leg':<18} {'trds':>5} {'t/yr':>5} {'WR':>6} {'E[R]':>7} "
          f"{'totalR':>8} {'R/yr':>6} {'Shrp':>5} {'Srtno':>6} {'maxDD':>6} "
          f"{'recov':>5} {'undwr':>5} {'h1R/y':>6} {'h2R/y':>6}  exit mix")


# ── runner ────────────────────────────────────────────────────────────────────

def run_study(legs, snapshot: Path, dump_dir: Path | None,
              start: date, wrt_label: str | None) -> None:
    with open(_ROOT / "config" / "filters.yaml", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f)
    with open(_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    with open(_ROOT / "config" / "watchlist.yaml", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = [t for t in wl.get("tier_a", wl.get("tickers", []))
               if isinstance(t, str)]

    exec_cfg = base_cfg.get("execution", {})
    base_port = {
        "max_open_risk": 5.0,
        "earnings_aware": True,
        "entry_slippage_pct": exec_cfg.get("entry_slippage_pct", 0.002),
        "commission_r": exec_cfg.get("commission_r", 0.005),
        "close_open_at_eod": True,
        "max_hold_days": int(exec_cfg.get("max_hold_days", 25)),
        "max_hold_mode": str(exec_cfg.get("max_hold_mode", "if_not_profit")),
    }
    # Shipped breakeven default rides along, exactly like run_backtest.
    if exec_cfg.get("breakeven_trigger_r"):
        base_port["breakeven_trigger_r"] = float(exec_cfg["breakeven_trigger_r"])
        if exec_cfg.get("breakeven_buffer_atr"):
            base_port["breakeven_buffer_atr"] = float(exec_cfg["breakeven_buffer_atr"])

    print(f"  Snapshot: {snapshot}", flush=True)
    uni = load_universe(
        tickers,
        ma_slow=base_cfg.get("trend", {}).get("ma_slow", 200),
        earnings_aware=True,
        cache_dir=snapshot / "prices",
        earnings_dir=snapshot / "earnings_history",
        macro_dir=snapshot / "macro",
        behavioral_dir=snapshot / "behavioral",
        start_date=start,
    )
    print(f"  {uni.summary()}", flush=True)
    print(f"\n{HEADER}", flush=True)

    for leg in legs:
        cfg = copy.deepcopy(base_cfg)
        for dotted, val in leg["cfg_mut"].items():
            _set_nested(cfg, dotted, val)
        port = dict(base_port)
        port.update(leg["port_mut"])

        engine = FilterEngine.from_dict(cfg)
        pcfg = PortfolioConfig(**port)
        bt = PortfolioBacktester(engine, pcfg)
        prepped = subset_tickers(uni.prepped, leg["tickers"])

        t0 = time.time()
        result = bt.run_prepped(
            prepped, uni.skipped, uni.market_dfs, uni.vix_df,
            macro_series=uni.macro_series,
            behavioral_data=uni.behavioral_data,
            spy_df=uni.spy_df,
            settings=settings,
        )
        trades = result.trades
        print(leg_row(leg["label"], trades) + f"  [{time.time() - t0:.0f}s]",
              flush=True)

        if dump_dir is not None and trades:
            dump_dir.mkdir(parents=True, exist_ok=True)
            safe = leg["label"].replace(",", "_").replace("=", "").replace("@", "_")
            pd.DataFrame([{
                "ticker": t.ticker, "entry_date": t.entry_date,
                "exit_date": t.exit_date, "exit_reason": t.exit_reason,
                "r_multiple": t.r_multiple, "effective_r": _eff_r(t),
                "size_mult": t.size_mult, "mfe_r": t.mfe_r, "mae_r": t.mae_r,
                "market_regime": t.market_regime, "signal_type": t.signal_type,
                "entry_price": t.entry_price, "initial_stop": t.initial_stop,
            } for t in trades]).to_parquet(dump_dir / f"{safe}.parquet")

        if wrt_label is not None and leg["label"] == wrt_label:
            print("\n  WR(T) ceiling — P(MFE ≥ T) from the baseline leg "
                  "(naive implied E[R]; ignores re-entry effects):", flush=True)
            for t_rung, p, naive in wrt_table(trades):
                print(f"    T={t_rung:<5g} would-be WR={p:>6.1%}   "
                      f"naive E[R]={naive:+.3f}", flush=True)
            print("", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--study", choices=["b1", "b2", "b3"], default=None)
    ap.add_argument("--legs", default=None,
                    help='custom legs: "label:key=val,...;label2:..."')
    ap.add_argument("--snapshot", default="data/snapshot_2026-06-10")
    ap.add_argument("--dump-trades", default=None, metavar="DIR")
    ap.add_argument("--start", default="2000-01-01")
    args = ap.parse_args()

    if not args.study and not args.legs:
        raise SystemExit("need --study or --legs")
    legs = study_legs(args.study) if args.study else []
    if args.legs:
        legs += parse_legs_spec(args.legs)
    wrt_label = "rr=2.5,mh=25" if args.study == "b2" else None
    dump = Path(args.dump_trades) if args.dump_trades else None
    run_study(legs, _ROOT / args.snapshot, dump,
              date.fromisoformat(args.start), wrt_label)


if __name__ == "__main__":
    main()
