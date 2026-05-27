"""
Backtest report renderer — terminal tables + standalone HTML.

Terminal
────────
    print_baseline(point, equity, bootstrap, kelly, attribution, streaks)
    print_equity_curve(ec)
    print_bootstrap(bootstrap)
    print_kelly(kelly, streaks)
    print_attribution(attribution)
    print_mean_rev_tune(report, baseline_er)
    print_walk_forward(wf)
    print_report(report)

HTML
────
    save_html(report, path, equity, wf_report, bootstrap, kelly, attribution, streaks)

CSV
───
    save_csv(report, out_dir)
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"

_USE_COLOR = os.isatty(1)


def _c(text, code):
    return f"{code}{text}{_RESET}" if _USE_COLOR else text


def _er_color(er, base):
    if er > base + 0.01: return _GREEN
    if er < base - 0.01: return _RED
    return _RESET


def _sparkbar(value, lo, hi, width=10):
    if hi <= lo: return " " * width
    pct = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


# ── terminal ──────────────────────────────────────────────────────────────────

def print_baseline(point, equity=None, bootstrap=None, kelly=None,
                   attribution=None, streaks=None, mc_dd=None):
    s = point.stats
    sep = "─" * 60
    print()
    print(_c("  TradAlert Backtester — Baseline Run", _BOLD + _CYAN))
    print(f"  {sep}")
    print(f"  Trades       : {s.trades_count:>6d}")
    print(f"  Win rate     : {s.win_rate:>6.1%}")
    print(f"  Expectancy   : {s.expectancy_r:>+7.3f} R")
    print(f"  Total R      : {s.total_r:>+7.1f} R")
    print(f"  Profit factor: {min(s.profit_factor, 999):>7.2f}")
    print(f"  Max drawdown : {s.max_drawdown_r:>7.2f} R")
    print(f"  Avg bars held: {s.avg_bars_held:>7.1f}")
    print(f"  Best trade   : {s.best_trade_r:>+7.2f} R")
    print(f"  Worst trade  : {s.worst_trade_r:>+7.2f} R")
    print(f"  {sep}")
    for label, breakdown in [
        ("By signal", point.by_signal), ("By regime", point.by_regime),
        ("By exit", point.by_exit), ("By year", point.by_year),
    ]:
        if not breakdown: continue
        print(f"\n  {_c(label, _BOLD)}")
        for key, st in sorted(breakdown.items()):
            bar = _sparkbar(st.expectancy_r, lo=-2.0, hi=2.0, width=10)
            print(f"    {key:<22s} {st.trades_count:4d}t  "
                  f"WR {st.win_rate:4.0%}  E[R] {st.expectancy_r:+.3f}  {bar}")
    if equity:    print_equity_curve(equity)
    if bootstrap: print_bootstrap(bootstrap)
    if kelly:     print_kelly(kelly, streaks)
    if mc_dd:     print_mc_drawdown(mc_dd)
    if attribution: print_attribution(attribution)
    print()


def print_equity_curve(ec):
    sep = "─" * 60
    print()
    print(_c("  Equity Curve Analytics", _BOLD + _CYAN))
    print(f"  {sep}")
    for line in ec.summary_lines():
        print(line)
    if not ec.annual.empty:
        print(f"\n  {_c('Annual P&L (R)', _BOLD)}")
        mx = max(abs(v) for v in ec.annual.values) or 1
        for yr, val in ec.annual.items():
            bar = _sparkbar(val, lo=-mx, hi=mx, width=20)
            col = _GREEN if val > 0 else _RED
            print(f"    {yr}  {_c(f'{val:+6.2f}R', col)}  {bar}")


def print_bootstrap(bootstrap, bankroll=50_000):
    sep = "─" * 60
    print()
    print(_c("  Bootstrap Confidence Intervals  (95%, n=10 000 resamples)", _BOLD + _CYAN))
    print(f"  {sep}")
    for m in ["expectancy", "win_rate", "total_r", "profit_factor"]:
        r = bootstrap.get(m)
        if r is None: continue
        sig = _c(" ✓", _GREEN) if r.significant else _c(" —", _RED)
        print(f"  {m:<16s} {r.estimate:>+8.3f}  "
              f"CI [{r.lower:>+7.3f} … {r.upper:>+7.3f}]  SE={r.std_error:.3f}{sig}")


def print_kelly(kelly, streaks=None, bankroll=50_000, fixed_risk_pct=0.01):
    """Print Kelly fractions + the operator-defined fixed-risk recommendation.

    ``fixed_risk_pct`` (default 0.01 = 1% of bankroll) is the *recommended*
    per-trade risk. Kelly fractions remain for reference but practical sizing
    should follow the 1% fixed-risk line.
    """
    sep = "─" * 60
    print()
    print(_c("  Position Sizing", _BOLD + _CYAN))
    print(f"  {sep}")
    print(f"  Fixed risk (rec.)    : {fixed_risk_pct:.1%}  "
          f"(${bankroll * fixed_risk_pct:>7,.0f} risk @ ${bankroll:,.0f})")
    print(f"  Full Kelly (ref.)    : {kelly.full_kelly:.1%}  "
          f"(${kelly.dollar_risk(bankroll, 'full'):>7,.0f} — usually too aggressive)")
    print(f"  Half Kelly (ref.)    : {kelly.half_kelly:.1%}  "
          f"(${kelly.dollar_risk(bankroll, 'half'):>7,.0f})")
    print(f"  Quarter Kelly (ref.) : {kelly.quarter_kelly:.1%}  "
          f"(${kelly.dollar_risk(bankroll, 'quarter'):>7,.0f})")
    print(f"  Edge per trade       : {kelly.edge_per_trade:+.3f} R")
    print(f"  Breakeven win rate   : {kelly.breakeven_wr:.1%}")
    if streaks:
        print(f"\n  {_c('Consecutive Loss Analysis', _BOLD)}")
        print(f"  Max losing streak : {streaks.max_consecutive}")
        print(f"  Avg losing streak : {streaks.avg_consecutive:.1f}")
        print(f"  P(streak ≥ 5)     : {streaks.p_streak_5:.1%}")
        p = streaks.binomial_at_least(kelly.win_rate, 5, 100)
        print(f"  P(≥5 losses in 100 trades, binomial): {p:.1%}")


def print_mc_drawdown(mc_dd, bankroll=50_000, risk_per_r_pct=0.01):
    """Default risk_per_r_pct=0.01 means 1 R risks 1% of the bankroll."""
    sep = "─" * 60
    risk_pct = risk_per_r_pct * 100
    print()
    print(_c("  Monte-Carlo Drawdown  (trade-order shuffling)", _BOLD + _CYAN))
    print(f"  {sep}")
    print("  Realized max DD      : see baseline above")
    print(f"  MC p50 (median)      : {mc_dd.p50:.2f} R")
    print(f"  MC p95 (size for)    : {mc_dd.p95:.2f} R  "
          f"(${mc_dd.p95 * bankroll * risk_per_r_pct:,.0f} @ "
          f"${bankroll:,.0f}, {risk_pct:.1f}% risk/R)")
    print(f"  MC p5 (best case)    : {mc_dd.p5:.2f} R")
    print(f"  Simulations          : {mc_dd.n_sim:,}")
    p95_pct = mc_dd.p95 * risk_pct  # % drawdown at configured risk per R
    flag = _c("  ✓ within 25% limit", _GREEN) if p95_pct < 25 else _c("  ✗ exceeds 25% — reduce size", _RED)
    print(f"  p95 as % account     : ~{p95_pct:.0f}%  "
          f"(at {risk_pct:.1f}% risk/R)  {flag}")


def print_attribution(attribution):
    sep = "─" * 60
    print()
    print(_c("  Per-Ticker Attribution (sorted by total R)", _BOLD + _CYAN))
    print(f"  {sep}")
    print(f"  {'Ticker':<8}  {'N':>4}  {'WR':>5}  {'E[R]':>7}  "
          f"{'Total R':>8}  {'Best':>6}  {'Worst':>6}")
    print("  " + "─" * 58)
    for row in attribution:
        er_c = _GREEN if row.expectancy_r > 0.05 else (_RED if row.expectancy_r < -0.05 else _RESET)
        tr_c = _GREEN if row.total_r > 0 else _RED
        print(f"  {row.ticker:<8}  {row.n_trades:>4}  {row.win_rate:>5.0%}  "
              f"{_c(f'{row.expectancy_r:>+7.3f}', er_c)}  "
              f"{_c(f'{row.total_r:>+8.2f}R', tr_c)}  "
              f"{row.best_r:>+6.2f}  {row.worst_r:>+6.2f}")
    total_r = sum(r.total_r for r in attribution)
    n_pos = sum(1 for r in attribution if r.total_r > 0)
    print(f"\n  {n_pos}/{len(attribution)} tickers profitable  |  "
          f"Combined total R: {total_r:+.2f}")


def print_mean_rev_tune(report, baseline_er):
    sep = "─" * 60
    print()
    print(_c("  Mean-Reversion Parameter Tuning", _BOLD + _CYAN))
    print(f"  {sep}")
    print(f"  Baseline E[R] (all signals): {baseline_er:+.3f}\n")
    for group, pts in sorted(report.by_group().items()):
        if not pts: continue
        print(_c(f"  ── {pts[0].param_label}", _BOLD))
        print(f"  {'Value':<10}  {'Trades':>6}  {'E[R]':>7}  "
              f"{'MR E[R]':>8}  {'MR trades':>9}  {'MR WR':>6}")
        print("  " + "─" * 56)
        for pt in sorted(pts, key=lambda p: (
                p.param_value if isinstance(p.param_value, (int, float)) else 0
        )):
            s = pt.stats
            mr = pt.by_signal.get("mean_reversion")
            mr_er = f"{mr.expectancy_r:>+8.3f}" if mr else "       N/A"
            mr_t = f"{mr.trades_count:>9}" if mr else "         —"
            mr_wr = f"{mr.win_rate:>6.0%}" if mr else "     —"
            er_c = _er_color(s.expectancy_r, baseline_er)
            mr_c = _er_color(mr.expectancy_r, 0) if mr else _RESET
            print(f"  {str(pt.param_value):<10}  {s.trades_count:>6}  "
                  f"{_c(f'{s.expectancy_r:>+7.3f}', er_c)}  "
                  f"{_c(mr_er, mr_c)}  {mr_t}  {mr_wr}")
        print()


def print_walk_forward(wf):
    for line in wf.summary_lines():
        print(line)


def print_robustness(results, base_er):
    sep = "─" * 60
    print()
    print(_c("  Parameter Robustness  (±10% / ±20% perturbation)", _BOLD + _CYAN))
    print(f"  {sep}")
    print(f"  Baseline E[R]: {base_er:+.3f}\n")

    # Group by param
    from collections import defaultdict
    groups = defaultdict(list)
    for r in results:
        groups[r["param"]].append(r)

    flagged = []
    for param, pts in sorted(groups.items()):
        pts_sorted = sorted(pts, key=lambda x: x["pct"])
        ers = [p["er"] for p in pts_sorted]
        er_range = max(ers) - min(ers) if ers else 0
        drop_from_base = base_er - min(ers) if base_er > 0 else 0
        drop_pct = (drop_from_base / abs(base_er) * 100) if base_er != 0 else 0

        flag = drop_pct > 50
        if flag:
            flagged.append((param, drop_pct))

        label = f"  {_c(param, _RED)}" if flag else f"  {param}"
        print(label)
        for p in pts_sorted:
            sign = "+" if p["pct"] > 0 else ""
            er_c = _er_color(p["er"], base_er)
            er_str = f"{p['er']:+.3f}"
            print(f"    {sign}{p['pct']:.0%} → {p['value']:>10g}  "
                  f"E[R]={_c(er_str, er_c)}  "
                  f"({p['trades']}t)")
        print(f"    range={er_range:.3f}  drop_from_base={drop_pct:.0f}%")
        print()

    if flagged:
        print(_c("  ⚠ FLAGGED — E[R] drops >50% from baseline:", _BOLD + _RED))
        for param, pct in flagged:
            print(f"    {param}: {pct:.0f}% drop")
    else:
        print(_c("  ✓ No params flagged — all within 50% drop threshold", _GREEN))
    print()


def print_report(report):
    print_baseline(report.baseline)
    base_er = report.baseline.stats.expectancy_r
    base_wr = report.baseline.stats.win_rate
    base_tr = report.baseline.stats.trades_count

    print(_c("  Parameter Sensitivity (sorted by E[R] spread)", _BOLD + _CYAN))
    sens = report.sensitivity()
    print(f"\n  {'Parameter':<32s} {'N':>2}  "
          f"{'E[R] best':>9}  {'E[R] worst':>10}  {'Spread':>7}")
    print("  " + "─" * 68)
    for _, row in sens.iterrows():
        bar = _sparkbar(row.er_spread, lo=0, hi=1.0, width=8)
        print(f"  {row['param']:<32s} {row.n_values:>2}  "
              f"{row.er_best:>+9.3f}  {row.er_worst:>+10.3f}  "
              f"{row.er_spread:>7.3f}  {bar}")

    print()
    for group, pts in sorted(report.by_group().items()):
        label = pts[0].param_label if pts else group
        print(_c(f"\n  ── {label} (group: {group})", _BOLD))
        _print_param_table(pts, base_er, base_wr, base_tr)

    top5 = report.best("expectancy_r", 5)
    if top5:
        print(_c("\n  ── Top 5 configurations by E[R]", _BOLD + _GREEN))
        print(f"\n  {'Rank':<5} {'Param':<32} {'Value':>8}  "
              f"{'Trades':>6}  {'WR':>5}  {'E[R]':>7}  {'TotalR':>8}")
        print("  " + "─" * 75)
        for i, pt in enumerate(top5, 1):
            print(f"  {i:<5} {pt.param_label:<32} {str(pt.param_value):>8}  "
                  f"{pt.stats.trades_count:>6}  {pt.stats.win_rate:>5.1%}  "
                  f"{pt.stats.expectancy_r:>+7.3f}  {pt.stats.total_r:>+8.1f}R")

    print(f"\n  Completed in {report.elapsed_s / 60:.1f} min "
          f"({len(report.points)} sweep points, {report.n_workers} workers)\n")


def _print_param_table(pts, base_er, base_wr, base_tr):
    print(f"\n  {'Value':<12}  {'Trades':>6}  {'WR':>5}  "
          f"{'E[R]':>7}  {'TotalR':>8}  {'MaxDD':>7}  {'PF':>6}  {'Bars':>5}")
    print("  " + "─" * 68)
    for pt in sorted(pts, key=lambda p: (
            p.param_value if isinstance(p.param_value, (int, float)) else 0
    )):
        s = pt.stats
        tag = " ★ base" if pt.is_baseline else ""
        er_c = _er_color(s.expectancy_r, base_er)
        pf_str = f"{min(s.profit_factor, 999):6.2f}"
        print(f"  {str(pt.param_value) + tag:<12}  {s.trades_count:>6}  {s.win_rate:>5.1%}  "
              f"{_c(f'{s.expectancy_r:+7.3f}', er_c)}  "
              f"{s.total_r:>+8.1f}R  {s.max_drawdown_r:>7.2f}  "
              f"{pf_str}  {s.avg_bars_held:>5.0f}")


# ── HTML ──────────────────────────────────────────────────────────────────────

def save_html(report, path, equity=None, wf_report=None,
              bootstrap=None, kelly=None, attribution=None, streaks=None,
              bankroll: float = 50_000, fixed_risk_pct: float = 0.01):
    """Render and write the HTML backtest report.

    ``bankroll`` and ``fixed_risk_pct`` mirror ``print_kelly`` defaults so
    the HTML Position Sizing block cannot disagree with the terminal one.
    """
    path = Path(path)
    html = _build_html(report, equity, wf_report, bootstrap, kelly,
                       attribution, streaks,
                       bankroll=bankroll, fixed_risk_pct=fixed_risk_pct)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _build_html(report, equity=None, wf_report=None,
                bootstrap=None, kelly=None, attribution=None, streaks=None,
                bankroll: float = 50_000, fixed_risk_pct: float = 0.01):
    base = report.baseline
    bs = base.stats
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_pts = len(report.points)
    sens = report.sensitivity()

    chart_labels = [f'"{r["param"]}"' for _, r in sens.iterrows()] if not sens.empty else []
    chart_best = [round(r["er_best"], 4) for _, r in sens.iterrows()] if not sens.empty else []
    chart_worst = [round(r["er_worst"], 4) for _, r in sens.iterrows()] if not sens.empty else []
    chart_spread = [round(r["er_spread"], 4) for _, r in sens.iterrows()] if not sens.empty else []

    group_tables_html = ""
    for group, pts in sorted(report.by_group().items()):
        label = pts[0].param_label if pts else group
        rows_html = ""
        for pt in sorted(pts, key=lambda p: (
                p.param_value if isinstance(p.param_value, (int, float)) else 0
        )):
            s = pt.stats
            er = s.expectancy_r
            cls = "win" if er > bs.expectancy_r + 0.01 else (
                "lose" if er < bs.expectancy_r - 0.01 else "")
            pf = min(s.profit_factor, 999)
            rows_html += f"""
            <tr class="{cls}">
              <td>{pt.param_value}</td><td>{s.trades_count}</td>
              <td>{s.win_rate:.1%}</td><td class="num">{er:+.3f}</td>
              <td class="num">{s.total_r:+.1f}R</td>
              <td class="num">{s.max_drawdown_r:.2f}R</td>
              <td class="num">{pf:.2f}</td><td>{s.avg_bars_held:.0f}</td>
            </tr>"""
        group_tables_html += f"""
        <section class="group-section">
          <h3>{_esc(label)} <span class="group-tag">{_esc(group)}</span></h3>
          <table><thead><tr>
            <th>Value</th><th>Trades</th><th>Win Rate</th>
            <th>E[R]</th><th>Total R</th><th>Max DD</th>
            <th>Prof. Factor</th><th>Avg Bars</th>
          </tr></thead><tbody>{rows_html}</tbody></table>
        </section>"""

    def _breakdown_html(label, breakdown):
        if not breakdown: return ""
        rows = ""
        for k, st in sorted(breakdown.items()):
            pf = min(st.profit_factor, 999)
            rows += f"""<tr>
              <td>{_esc(k)}</td><td>{st.trades_count}</td>
              <td>{st.win_rate:.1%}</td><td class="num">{st.expectancy_r:+.3f}</td>
              <td class="num">{st.total_r:+.1f}R</td><td class="num">{pf:.2f}</td>
            </tr>"""
        return f"""
        <section class="breakdown-section">
          <h3>{_esc(label)}</h3>
          <table><thead><tr>
            <th>Bucket</th><th>Trades</th><th>Win Rate</th>
            <th>E[R]</th><th>Total R</th><th>Prof. Factor</th>
          </tr></thead><tbody>{rows}</tbody></table>
        </section>"""

    breakdowns_html = (
            _breakdown_html("By Signal Type", base.by_signal)
            + _breakdown_html("By Market Regime", base.by_regime)
            + _breakdown_html("By Exit Reason", base.by_exit)
            + _breakdown_html("By Year", base.by_year)
    )

    # ── build sweep sections (hidden when baseline-only) ──────────────────────
    top5 = report.best("expectancy_r", 5)
    top5_rows = ""
    for i, pt in enumerate(top5, 1):
        s = pt.stats
        top5_rows += f"""<tr>
          <td>{i}</td><td>{_esc(pt.param_label)}</td><td>{pt.param_value}</td>
          <td>{s.trades_count}</td><td>{s.win_rate:.1%}</td>
          <td class="num">{s.expectancy_r:+.3f}</td>
          <td class="num">{s.total_r:+.1f}R</td>
        </tr>"""

    pf_display = f"{min(bs.profit_factor, 999):.2f}"

    # ── sweep sections (hidden when n_pts == 0) ────────────────────────────────
    if n_pts > 0:
        sensitivity_section = f"""
<h2>Parameter Sensitivity <span class="group-tag">{n_pts} sweep points</span></h2>
<div class="chart-wrapper">
  <canvas class="chart-canvas" id="sensChart"></canvas>
</div>"""
        sweep_tables_section = f"""
<h2>Parameter Sweep Tables</h2>
{group_tables_html}
<div class="top5-wrapper">
  <h3>Top 5 by Expectancy R</h3>
  <table><thead><tr>
    <th>#</th><th>Parameter</th><th>Value</th>
    <th>Trades</th><th>WR</th><th>E[R]</th><th>Total R</th>
  </tr></thead><tbody>{top5_rows}</tbody></table>
</div>"""
        sens_chart_init = f"""
  if(labels.length > 0){{
    new Chart(document.getElementById("sensChart"),{{
      type:"bar",
      data:{{labels:labels,datasets:[
        {{label:"Best E[R]",data:best,backgroundColor:"rgba(34,197,94,0.7)"}},
        {{label:"Worst E[R]",data:worst,backgroundColor:"rgba(239,68,68,0.7)"}},
        {{label:"Spread",data:spread,type:"line",yAxisID:"y2",fill:false,
          borderColor:"rgba(99,102,241,0.9)",tension:0.3}}
      ]}},
      options:{{responsive:true,maintainAspectRatio:false,
        plugins:{{legend:{{labels:{{color:"#e2e8f0"}}}}}},
        scales:{{
          x:{{ticks:{{color:"#94a3b8"}},grid:{{color:"#2a2d3a"}}}},
          y:{{ticks:{{color:"#94a3b8"}},grid:{{color:"#2a2d3a"}},
             title:{{display:true,text:"E[R]",color:"#94a3b8"}}}},
          y2:{{position:"right",ticks:{{color:"#94a3b8"}},
               grid:{{drawOnChartArea:false}},
               title:{{display:true,text:"Spread",color:"#94a3b8"}}}}
        }}
      }}
    }});
  }}"""
    else:
        sensitivity_section = ""
        sweep_tables_section = ""
        sens_chart_init = ""

    # equity section
    equity_html = ""
    equity_js = ""
    if equity and not equity.equity.empty:
        eq_dates = [str(d.date()) for d in equity.equity.index]
        eq_vals = [round(float(v), 4) for v in equity.equity.values]
        dd_vals = [round(float(v), 4) for v in equity.drawdown.values]
        # Backfill zero-trade months so silent regimes (e.g. the
        # Mar-May 2025 tariff-scare gap from the 2026-05-27 postmortem)
        # show up explicitly in the bar chart. Renders zero months grey
        # so they're distinguishable from real flat months.
        _mo_keys = list(equity.monthly.index)
        if _mo_keys:
            _first = _mo_keys[0]
            _last = _mo_keys[-1]
            _fy, _fm = int(_first[:4]), int(_first[5:7])
            _ly, _lm = int(_last[:4]), int(_last[5:7])
            _all_keys: list[str] = []
            _y, _m = _fy, _fm
            while (_y, _m) <= (_ly, _lm):
                _all_keys.append(f"{_y:04d}-{_m:02d}")
                _m += 1
                if _m > 12:
                    _y += 1;
                    _m = 1
        else:
            _all_keys = []
        _existing = {str(k): float(v) for k, v in equity.monthly.items()}
        mo_labels = _all_keys
        mo_vals = [round(_existing.get(k, 0.0), 4) for k in _all_keys]

        def _mo_color(k: str, v: float) -> str:
            if k not in _existing:  # zero-trade month
                return "rgba(100,116,139,0.45)"  # var(--muted)-ish grey
            if v >= 0:
                return "rgba(34,197,94,0.8)"
            return "rgba(239,68,68,0.8)"

        mo_colors = [_mo_color(k, v) for k, v in zip(mo_labels, mo_vals)]
        sharpe_s = f"{equity.sharpe:.2f}" if equity.sharpe == equity.sharpe else "N/A"
        sortino_s = f"{equity.sortino:.2f}" if equity.sortino == equity.sortino else "N/A"
        calmar_s = f"{equity.calmar:.2f}" if equity.calmar != float("inf") and equity.calmar == equity.calmar else "inf"
        bm, bv = equity.best_month
        wm, wv = equity.worst_month
        eq_dates_js = str(eq_dates)
        eq_vals_js = str(eq_vals)
        dd_vals_js = str(dd_vals)
        mo_labels_js = str(mo_labels)
        mo_vals_js = str(mo_vals)
        mo_colors_js = str(mo_colors)
        sh_cls = "green" if equity.sharpe > 1 else ("yellow" if equity.sharpe > 0 else "red")
        so_cls = "green" if equity.sortino > 1.5 else "yellow"
        ca_cls = "green" if equity.calmar > 0.5 else "yellow"
        pm_cls = "green" if equity.pct_positive_months >= 0.55 else "yellow"
        ar_cls = "green" if equity.annual_r > 0 else "red"

        equity_html = f"""
<h2>Equity Curve &amp; Risk</h2>
<div class="stats-grid">
  {_card("Sharpe (ann.)", sharpe_s, sh_cls)}
  {_card("Sortino (ann.)", sortino_s, so_cls)}
  {_card("Calmar Ratio", calmar_s, ca_cls)}
  {_card("Peak Drawdown", f"{equity.max_dd:.2f} R", "red")}
  {_card("DD Recovery", f"{equity.recovery_days or 'ongoing'} days", "blue")}
  {_card("Positive Months", f"{equity.pct_positive_months:.0%}", pm_cls)}
  {_card("Best Month", f"{bm}  {bv:+.2f} R", "green")}
  {_card("Worst Month", f"{wm}  {wv:+.2f} R", "red")}
  {_card("Annual Avg R", f"{equity.annual_r:+.2f} R/yr", ar_cls)}
</div>
<div class="chart-wrapper"><canvas class="chart-canvas" id="eqChart"></canvas></div>
<div class="chart-wrapper"><canvas class="chart-canvas" id="moChart"></canvas></div>
<div class="chart-wrapper" style="max-height:200px"><canvas class="chart-canvas" id="ddChart"></canvas></div>"""

        equity_js = f"""(function(){{
  var eqD={eq_dates_js}, eqV={eq_vals_js}, ddV={dd_vals_js};
  var moL={mo_labels_js}, moV={mo_vals_js}, moC={mo_colors_js};
  var mkChart=function(id,type,labels,datasets,extra){{
    new Chart(document.getElementById(id),{{type:type,data:{{labels:labels,datasets:datasets}},options:Object.assign({{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{labels:{{color:"#e2e8f0"}}}}}},scales:{{x:{{ticks:{{color:"#64748b",maxTicksLimit:16}},grid:{{color:"#2a2d3a"}}}},y:{{ticks:{{color:"#94a3b8"}},grid:{{color:"#2a2d3a"}}}}}}}},extra)}});}};
  mkChart("eqChart","line",eqD,[{{label:"Cumulative R",data:eqV,borderColor:"rgba(99,102,241,0.9)",backgroundColor:"rgba(99,102,241,0.1)",fill:true,pointRadius:0,tension:0.2}}],{{plugins:{{title:{{display:true,text:"Equity Curve (R)",color:"#94a3b8"}}}}}});
  mkChart("moChart","bar",moL,[{{label:"Monthly R",data:moV,backgroundColor:moC,borderRadius:3}}],{{plugins:{{title:{{display:true,text:"Monthly P&L (R)",color:"#94a3b8"}}}}}});
  mkChart("ddChart","line",eqD,[{{label:"Drawdown",data:ddV,borderColor:"rgba(239,68,68,0.8)",backgroundColor:"rgba(239,68,68,0.15)",fill:true,pointRadius:0,tension:0.2}}],{{plugins:{{title:{{display:true,text:"Drawdown from Peak (R)",color:"#94a3b8"}}}},scales:{{y:{{reverse:true}}}}}});
}})();"""

    # bootstrap section
    bootstrap_html = ""
    if bootstrap:
        rows = ""
        for m, r in bootstrap.items():
            cls = "win" if r.significant and r.estimate > 0 else (
                "lose" if r.significant and r.estimate < 0 else "")
            rows += f"""<tr class="{cls}">
              <td>{_esc(m)}</td>
              <td class="num">{r.estimate:+.4f}</td><td class="num">{r.lower:+.4f}</td>
              <td class="num">{r.upper:+.4f}</td><td class="num">{r.std_error:.4f}</td>
              <td>{"✓ significant" if r.significant else "—"}</td>
            </tr>"""
        bootstrap_html = f"""
<h2>Bootstrap CIs <span class="group-tag">95% · 10 000 resamples</span></h2>
<div class="top5-wrapper"><table>
  <thead><tr><th>Metric</th><th>Estimate</th><th>CI Lower</th><th>CI Upper</th>
  <th>SE</th><th>Significant</th></tr></thead>
  <tbody>{rows}</tbody>
</table></div>"""

    # ── Position Sizing & Kelly ───────────────────────────────────────────
    # Operator-facing layout: 1% fixed-risk recommendation leads; Kelly is
    # demoted to a labelled reference table. Mirrors print_kelly() so the
    # HTML report cannot disagree with the terminal report.
    kelly_html = ""
    if kelly:
        risk_dollars = bankroll * fixed_risk_pct
        expected_dollars = kelly.edge_per_trade * risk_dollars

        # 1. Recommended sizing — the only block the operator should act on.
        recommend_table = (
            '<p class="subhead">Recommended sizing — fixed risk</p>'
            '<table>'
            '<thead><tr><th>Metric</th><th>Value</th></tr></thead>'
            '<tbody>'
            f'<tr class="recommend"><td>Risk per trade</td>'
            f'<td class="num">${risk_dollars:,.0f}  '
            f'({fixed_risk_pct:.1%} of ${bankroll:,.0f})</td></tr>'
            f'<tr><td>Expected per trade</td>'
            f'<td class="num">{expected_dollars:+,.0f}$  '
            f'(edge {kelly.edge_per_trade:+.3f} R)</td></tr>'
            f'<tr><td>Max one-trade loss (1R)</td>'
            f'<td class="num">-${risk_dollars:,.0f}</td></tr>'
            '</tbody></table>'
        )

        # 2. Edge reality check — does the strategy have meaningful slack?
        buffer_pp = (kelly.win_rate - kelly.breakeven_wr) * 100.0
        buffer_class = "win" if buffer_pp >= 5.0 else ("lose" if buffer_pp < 0 else "warn")
        edge_table = (
            '<p class="subhead">Edge reality check</p>'
            '<table>'
            '<thead><tr><th>Metric</th><th>Value</th></tr></thead>'
            '<tbody>'
            f'<tr><td>Win rate observed</td>'
            f'<td class="num">{kelly.win_rate:.1%}</td></tr>'
            f'<tr><td>Breakeven WR needed</td>'
            f'<td class="num">{kelly.breakeven_wr:.1%}</td></tr>'
            f'<tr class="{buffer_class}"><td>Buffer above breakeven</td>'
            f'<td class="num">{buffer_pp:+.1f} pp</td></tr>'
            f'<tr><td>Edge per trade</td>'
            f'<td class="num">{kelly.edge_per_trade:+.4f} R</td></tr>'
            f'<tr><td>Avg winner / Avg loser</td>'
            f'<td class="num">{kelly.avg_win_r:+.2f}R / -{kelly.avg_loss_r:.2f}R</td></tr>'
            '</tbody></table>'
        )

        # 3. Loss-streak stress test — sets expectations for run-of-losses pain.
        streak_table = ""
        if streaks:
            streak_dollars = streaks.max_consecutive * risk_dollars
            binom_5_100 = streaks.binomial_at_least(kelly.win_rate, 5, 100)
            streak_table = (
                '<p class="subhead">Loss-streak stress test</p>'
                '<table>'
                '<thead><tr><th>Metric</th><th>Value</th></tr></thead>'
                '<tbody>'
                f'<tr><td>Max observed streak</td>'
                f'<td class="num">{streaks.max_consecutive} trades '
                f'(-${streak_dollars:,.0f})</td></tr>'
                f'<tr><td>Avg losing streak</td>'
                f'<td class="num">{streaks.avg_consecutive:.1f} trades</td></tr>'
                f'<tr><td>P(streak ≥ 5) — empirical</td>'
                f'<td class="num">{streaks.p_streak_5:.1%}</td></tr>'
                f'<tr><td>P(≥5 losses in 100 trades) — binomial</td>'
                f'<td class="num">{binom_5_100:.1%}</td></tr>'
                '</tbody></table>'
            )

        # 4. Kelly reference — labelled DO NOT USE, kept for analysis only.
        kelly_full_dollars = kelly.dollar_risk(bankroll, "full")
        kelly_half_dollars = kelly.dollar_risk(bankroll, "half")
        kelly_qtr_dollars = kelly.dollar_risk(bankroll, "quarter")
        kelly_reference = (
            '<p class="subhead">Kelly reference — do <strong>not</strong> size with these</p>'
            '<p class="note warn">⚠  Kelly is mathematically optimal under stationary '
            'edge; real markets are not stationary. Use the 1% fixed-risk line above; '
            'keep Kelly fractions only for sanity-checking that the recommended size '
            'is well below Full Kelly.</p>'
            '<table>'
            '<thead><tr><th>Fraction</th><th>Of bankroll</th>'
            '<th>Dollars at risk</th></tr></thead>'
            '<tbody>'
            f'<tr class="warn"><td>Full Kelly</td>'
            f'<td class="num">{kelly.full_kelly:.1%}</td>'
            f'<td class="num">${kelly_full_dollars:,.0f}</td></tr>'
            f'<tr class="warn"><td>Half Kelly</td>'
            f'<td class="num">{kelly.half_kelly:.1%}</td>'
            f'<td class="num">${kelly_half_dollars:,.0f}</td></tr>'
            f'<tr class="warn"><td>Quarter Kelly</td>'
            f'<td class="num">{kelly.quarter_kelly:.1%}</td>'
            f'<td class="num">${kelly_qtr_dollars:,.0f}</td></tr>'
            '</tbody></table>'
        )

        kelly_html = (
            '<h2>Position Sizing &amp; Kelly</h2>'
            '<div class="top5-wrapper">'
            f'{recommend_table}'
            f'{edge_table}'
            f'{streak_table}'
            f'{kelly_reference}'
            '</div>'
        )

    # ── Stop-out latency histogram ────────────────────────────────────────
    # Postmortem 2026-05-27 found 11 of 36 stops failed within 3 bars
    # (-12.9R, 26 % of stop damage). Surfacing the distribution here makes
    # bad-entry latency visible at a glance for future runs.
    stop_latency_html = ""
    try:
        _stop_trades = [
            t for t in getattr(report.baseline, "trades", [])
            if getattr(t, "exit_reason", "") == "stop"
        ]
    except (AttributeError, TypeError):
        _stop_trades = []
    if _stop_trades:
        _buckets = [(0, 2, "0-2"), (3, 5, "3-5"),
                    (6, 10, "6-10"), (11, 20, "11-20"),
                    (21, 10_000, "21+")]
        _total_n = len(_stop_trades)
        _total_r = float(sum(t.effective_r for t in _stop_trades))
        _rows = []
        for lo, hi, label in _buckets:
            _bucket = [
                t for t in _stop_trades
                if lo <= int(getattr(t, "bars_held", 0) or 0) <= hi
            ]
            n = len(_bucket)
            if n == 0:
                continue
            sumR = float(sum(t.effective_r for t in _bucket))
            share_n = (n / _total_n) * 100.0
            share_r = (sumR / _total_r) * 100.0 if _total_r else 0.0
            row_cls = "lose" if share_r >= 25.0 else ""
            _rows.append(
                f'<tr class="{row_cls}">'
                f'<td>{label} bars</td>'
                f'<td class="num">{n}</td>'
                f'<td class="num">{share_n:.0f}%</td>'
                f'<td class="num">{sumR:+.2f} R</td>'
                f'<td class="num">{share_r:.0f}%</td>'
                f'</tr>'
            )
        stop_latency_html = (
            '<h2>Stop-out Latency '
            f'<span class="group-tag">{_total_n} stops · '
            f'{_total_r:+.1f} R total</span></h2>'
            '<div class="top5-wrapper"><table>'
            '<thead><tr><th>Bars held</th><th>Trades</th><th>Share</th>'
            '<th>Total R</th><th>R share</th></tr></thead>'
            f'<tbody>{"".join(_rows)}</tbody></table>'
            '<p class="note">Rows highlighted red carry ≥25 % of total '
            'stop damage — anti-gap entry (--anti-gap-entry) targets the '
            "&quot;0-2 bars&quot; cluster directly.</p></div>"
        )

    # walk-forward section
    wf_html = ""
    if wf_report:
        wf_rows = ""
        for r in wf_report.results:
            cls = "win" if r.oos_positive else "lose"
            wf_rows += f"""<tr class="{cls}">
              <td>W{r.window.index:02d}</td>
              <td>{r.window.is_start}→{r.window.is_end}</td>
              <td>{r.window.oos_start}→{r.window.oos_end}</td>
              <td class="num">{r.is_point.stats.trades_count}</td>
              <td class="num">{r.is_er:+.3f}</td>
              <td class="num">{r.oos_point.stats.trades_count}</td>
              <td class="num">{r.oos_er:+.3f}</td>
              <td class="num">{r.degradation:+.3f}</td>
              <td>{"✓" if r.oos_positive else "✗"}</td>
            </tr>"""
        dg_cls = "red" if wf_report.degradation > 0.1 else "green"
        wf_html = f"""
<h2>Walk-Forward <span class="group-tag">{wf_report.is_years:.0f}yr IS / {wf_report.oos_years:.0f}yr OOS</span></h2>
<div class="stats-grid">
  {_card("Windows", str(len(wf_report.results)), "blue")}
  {_card("Avg IS E[R]", f"{wf_report.avg_is_er:+.3f}", "blue")}
  {_card("Avg OOS E[R]", f"{wf_report.avg_oos_er:+.3f}", "green" if wf_report.avg_oos_er > 0 else "red")}
  {_card("Degradation", f"{wf_report.degradation:+.3f}", dg_cls)}
  {_card("OOS Profitable", f"{wf_report.pct_oos_positive:.0%}", "green" if wf_report.pct_oos_positive >= 0.6 else "yellow")}
</div>
<div class="top5-wrapper"><table>
  <thead><tr><th>Win</th><th>IS Period</th><th>OOS Period</th>
  <th>IS Trades</th><th>IS E[R]</th><th>OOS Trades</th><th>OOS E[R]</th>
  <th>Degradation</th><th>OOS+</th></tr></thead>
  <tbody>{wf_rows}</tbody>
</table></div>"""

    # attribution section
    attr_html = ""
    if attribution:
        attr_rows = ""
        for row in attribution:
            cls = "win" if row.total_r > 0 else "lose"
            attr_rows += f"""<tr class="{cls}">
              <td>{_esc(row.ticker)}</td>
              <td class="num">{row.n_trades}</td><td class="num">{row.win_rate:.0%}</td>
              <td class="num">{row.expectancy_r:+.3f}</td>
              <td class="num">{row.total_r:+.2f}R</td>
              <td class="num">{row.best_r:+.2f}</td>
              <td class="num">{row.worst_r:+.2f}</td>
            </tr>"""
        n_pos = sum(1 for r in attribution if r.total_r > 0)
        attr_html = f"""
<h2>Per-Ticker Attribution <span class="group-tag">{n_pos}/{len(attribution)} profitable</span></h2>
<div class="top5-wrapper"><table>
  <thead><tr><th>Ticker</th><th>Trades</th><th>WR</th>
  <th>E[R]</th><th>Total R</th><th>Best</th><th>Worst</th></tr></thead>
  <tbody>{attr_rows}</tbody>
</table></div>"""

    css = """
:root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
  --muted:#64748b;--accent:#6366f1;--green:#22c55e;--red:#ef4444;
  --yellow:#f59e0b;--card-bg:#1e2130;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  font-size:14px;line-height:1.6;}
.container{max-width:1280px;margin:0 auto;padding:32px 24px;}
h1{font-size:1.8rem;font-weight:700;color:var(--accent);margin-bottom:4px;}
.subtitle{color:var(--muted);margin-bottom:32px;}
h2{font-size:1.2rem;font-weight:600;color:var(--text);
   margin:40px 0 16px;border-left:3px solid var(--accent);padding-left:12px;}
h3{font-size:1rem;font-weight:600;margin-bottom:12px;color:var(--accent);}
.group-tag{font-size:0.7rem;background:#2a2d3a;padding:2px 8px;
  border-radius:4px;color:var(--muted);margin-left:8px;}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));
  gap:16px;margin-bottom:32px;}
.stat-card{background:var(--card-bg);border:1px solid var(--border);
  border-radius:10px;padding:16px;}
.stat-label{font-size:0.75rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:0.05em;}
.stat-value{font-size:1.5rem;font-weight:700;margin-top:4px;}
.stat-value.green{color:var(--green);}.stat-value.red{color:var(--red);}
.stat-value.blue{color:#60a5fa;}.stat-value.yellow{color:var(--yellow);}
table{width:100%;border-collapse:collapse;margin-bottom:8px;
  background:var(--surface);border-radius:8px;overflow:hidden;}
thead th{background:#252836;color:var(--muted);font-size:0.75rem;
  text-transform:uppercase;letter-spacing:0.05em;
  padding:10px 12px;text-align:left;font-weight:500;}
tbody td{padding:9px 12px;border-top:1px solid var(--border);}
tbody tr:hover{background:rgba(99,102,241,0.05);}
tbody tr.win{border-left:3px solid var(--green);}
tbody tr.lose{border-left:3px solid var(--red);}
tbody tr.recommend{background:rgba(34,197,94,0.08);border-left:3px solid var(--green);font-weight:600;}
tbody tr.warn td:first-child{color:var(--yellow);}
.note{color:var(--muted);font-size:0.8rem;margin:4px 0 18px 0;font-style:italic;}
.note.warn{color:var(--yellow);font-style:normal;font-weight:500;}
.subhead{color:var(--muted);font-size:0.85rem;text-transform:uppercase;letter-spacing:0.07em;
  margin:14px 0 6px 0;}
.num{font-variant-numeric:tabular-nums;text-align:right;font-family:monospace;}
.group-section,.breakdown-section{margin-bottom:32px;}
.chart-wrapper{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:24px;margin-bottom:32px;}
.chart-canvas{max-height:320px;}
.top5-wrapper{background:var(--card-bg);border:1px solid var(--border);
  border-radius:10px;padding:24px;margin-bottom:32px;}
.meta{color:var(--muted);font-size:0.8rem;margin-top:32px;
  border-top:1px solid var(--border);padding-top:16px;}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradAlert Backtest — {ts}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>{css}</style>
</head>
<body>
<div class="container">
<h1>TradAlert Backtest Report</h1>
<p class="subtitle">Generated {ts} &nbsp;·&nbsp; {_esc(report.universe_info)} &nbsp;·&nbsp; {n_pts} sweep points &nbsp;·&nbsp; {report.elapsed_s / 60:.1f} min</p>

<h2>Baseline Performance</h2>
<div class="stats-grid">
  {_card("Trades", str(bs.trades_count), "blue")}
  {_card("Win Rate", f"{bs.win_rate:.1%}", "green" if bs.win_rate >= 0.5 else "red")}
  {_card("Expectancy R", f"{bs.expectancy_r:+.3f}R", "green" if bs.expectancy_r > 0 else "red")}
  {_card("Total R", f"{bs.total_r:+.1f}R", "green" if bs.total_r > 0 else "red")}
  {_card("Profit Factor", pf_display, "green" if bs.profit_factor >= 1.5 else ("yellow" if bs.profit_factor >= 1 else "red"))}
  {_card("Max Drawdown", f"{bs.max_drawdown_r:.2f}R", "red")}
  {_card("Avg Bars", f"{bs.avg_bars_held:.0f}", "blue")}
  {_card("Best Trade", f"{bs.best_trade_r:+.2f}R", "green")}
  {_card("Worst Trade", f"{bs.worst_trade_r:+.2f}R", "red")}
</div>

{equity_html}
{stop_latency_html}
{bootstrap_html}
{kelly_html}

<h2>Breakdowns (Baseline)</h2>
{breakdowns_html}

{wf_html}
{attr_html}

{sensitivity_section}
<script>
(function(){{
  var labels=[{", ".join(chart_labels)}];
  var best=[{", ".join(str(x) for x in chart_best)}];
  var worst=[{", ".join(str(x) for x in chart_worst)}];
  var spread=[{", ".join(str(x) for x in chart_spread)}];
  {sens_chart_init}
}})();
{equity_js}
</script>

{sweep_tables_section}

<p class="meta">TradAlert · {ts} · Universe: {_esc(report.universe_info)}</p>
</div>
</body>
</html>"""


def _card(label, value, cls=""):
    return (f'<div class="stat-card">'
            f'<div class="stat-label">{_esc(label)}</div>'
            f'<div class="stat-value {cls}">{_esc(value)}</div>'
            f'</div>')


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── CSV ───────────────────────────────────────────────────────────────────────

def save_csv(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sp = out_dir / "sweep_results.csv"
    tp = out_dir / "trades.csv"
    report.to_dataframe().to_csv(sp, index=False)
    report.trades_dataframe().to_csv(tp, index=False)
    return sp, tp
