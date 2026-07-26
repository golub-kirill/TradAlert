import { Card, Note } from "../components/Card";
import { Kpis, type KpiItem } from "../components/Kpi";
import { PerformanceChart } from "../components/PerformanceChart";
import { useApi } from "../hooks/useApi";
import { getBacktests, getMonthly, getPositions, getScannerLatest } from "../api/client";
import { fnum, pct, rstr, signClass } from "../lib/format";

const MISMATCH_SHOWN = 3;

// Mirrors backtest.db.reference_caveat: say when the list is truncated, so three
// shown keys are never mistaken for three total.
function mismatchText(keys: string[] | undefined): string {
  const list = keys ?? [];
  if (!list.length) return "";
  const head = list.slice(0, MISMATCH_SHOWN).join("; ");
  return list.length > MISMATCH_SHOWN
    ? `${head}; … and ${list.length - MISMATCH_SHOWN} more`
    : head;
}

export function Overview() {
  // Fetch several, not one: the newest journaled run is often an A/B leg, and
  // headlining it puts an experiment arm's numbers on the dashboard as though
  // they described the strategy. (Run 34 — a VIX-slope-gated leg — showed 44.32R
  // here next to the baseline's 90.83R, which reads as a collapse in the edge.)
  // More than one, but not twenty: the newest journaled run is often an A/B leg,
  // and headlining it puts an experiment arm's numbers on the dashboard as though
  // they described the strategy. (Run 34 — a VIX-slope-gated leg — showed 44.32R
  // here next to the baseline's 90.83R, which reads as a collapse in the edge.)
  // Five covers any realistic A/B streak without shipping unused param diffs.
  const runs = useApi(() => getBacktests(5), []);
  const positions = useApi(getPositions, []);
  const scan = useApi(getScannerLatest, []);

  const all = runs.data ?? [];
  const newest = all[0];
  // Newest run that measured the shipped config. `null` means "no snapshot to
  // check" and stays eligible — refusing those would leave no baseline at all.
  const r = all.find((x) => x.config_match !== false) ?? newest;
  const latestId = r?.id;
  // Warn when we stepped over a run to find a baseline, AND when there was no
  // baseline to find — otherwise a run of nothing but A/B legs headlines an
  // experiment arm in silence, which is the failure this exists to prevent.
  const skipped = newest && r && newest.id !== r.id ? newest : undefined;
  const unmatched = r?.config_match === false ? r : undefined;
  const monthly = useApi(() => (latestId ? getMonthly(latestId) : Promise.resolve(null)), [latestId]);
  const kpis: KpiItem[] = [
    { label: "Net total R", value: fnum(r?.total_r, 2), tone: (r?.total_r ?? 0) >= 0 ? "pos" : "neg" },
    { label: "Win rate", value: pct(r?.win_rate, 1) },
    { label: "Profit factor", value: fnum(r?.profit_factor, 2) },
    { label: "Trades", value: r?.trades_count ?? "—" },
    { label: "Expectancy", value: fnum(r?.expectancy_r, 3) },
    { label: "Max DD R", value: fnum(r?.max_drawdown_r, 1), tone: "warn" },
  ];

  const pos = positions.data ?? [];
  const run = scan.data?.run;

  return (
    <>
      {unmatched ? (
        <div className="banner warn" role="status">
          <i className="ti ti-alert-triangle" />
          <div>
            <strong>
              No baseline run available — these numbers are from an A/B leg.
            </strong>
            <div className="mut" style={{ marginTop: 4 }}>
              Run #{unmatched.id} ran a different config from the shipped{" "}
              <code>filters.yaml</code>, and no recent run matched it, so the figures below
              describe a different strategy. Re-journal a full-window baseline.{" "}
              {mismatchText(unmatched.config_mismatch)}
            </div>
          </div>
        </div>
      ) : skipped ? (
        <div className="banner warn" role="status">
          <i className="ti ti-flask" />
          <div>
            <strong>
              Showing run #{latestId} — the newest baseline, not the newest run.
            </strong>
            <div className="mut" style={{ marginTop: 4 }}>
              Run #{skipped.id} ({fnum(skipped.total_r, 2)}R) ran a different config from the
              shipped <code>filters.yaml</code>, so it measured a different strategy — an A/B
              leg, not the edge. {mismatchText(skipped.config_mismatch)}
            </div>
          </div>
        </div>
      ) : null}
      <Kpis items={kpis} />

      <Card
        title={"Performance" + (latestId ? ` · run #${latestId} (R)` : "")}
        icon="ti-chart-candle"
        right={
          monthly.data ? (
            <span className="mut" style={{ fontSize: 12 }}>
              Win {pct(monthly.data.win_rate)} · Up months {pct(monthly.data.up_month_pct)}
            </span>
          ) : undefined
        }
      >
        {monthly.loading ? (
          <Note>Loading…</Note>
        ) : !monthly.data || monthly.data.months.length === 0 ? (
          <Note>No trades to chart for the latest run.</Note>
        ) : (
          <PerformanceChart months={monthly.data.months} />
        )}
      </Card>

      <div className="grid2">
        <Card title="Open positions" icon="ti-briefcase">
          {pos.length === 0 ? (
            <Note>No open positions.</Note>
          ) : (
            pos.slice(0, 6).map((p) => (
              <div className="row" key={p.id}>
                <span>
                  {p.ticker} <span className="mut">{p.side}</span>
                </span>
                <span className={signClass(p.unrealized_r)}>{rstr(p.unrealized_r)}</span>
              </div>
            ))
          )}
        </Card>

        <Card title="Latest scan" icon="ti-radar">
          {!run ? (
            <Note>No scans journaled yet.</Note>
          ) : (
            <>
              <div className="row">
                <span className="mut">Run</span>
                <span>#{run.run_id}</span>
              </div>
              <div className="row">
                <span className="mut">Scanned / passed</span>
                <span>
                  {run.tickers_scanned} / {run.scan_passed}
                </span>
              </div>
              <div className="row">
                <span className="mut">Fired</span>
                <span className="pos">{run.signals_fired}</span>
              </div>
              <div className="row">
                <span className="mut">Regime</span>
                <span>{run.market_regime ?? "—"}</span>
              </div>
            </>
          )}
        </Card>
      </div>
    </>
  );
}
