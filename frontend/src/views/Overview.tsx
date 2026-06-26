import { Card, Note } from "../components/Card";
import { Kpis, type KpiItem } from "../components/Kpi";
import { Sparkline } from "../components/Sparkline";
import { useApi } from "../hooks/useApi";
import { getBacktests, getEquity, getPositions, getScannerLatest } from "../api/client";
import { fnum, pct, rstr, signClass } from "../lib/format";

export function Overview() {
  const runs = useApi(() => getBacktests(1), []);
  const positions = useApi(getPositions, []);
  const scan = useApi(getScannerLatest, []);

  const r = runs.data?.[0];
  const latestId = r?.id;
  const eq = useApi(
    () => (latestId ? getEquity(latestId) : Promise.resolve({ run_id: 0, points: [] })),
    [latestId],
  );
  const eqVals = (eq.data?.points ?? []).map((p) => p.equity_r);
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
      <Kpis items={kpis} />

      <Card title={"Equity curve" + (latestId ? ` · run #${latestId} (R)` : "")} icon="ti-trending-up">
        {eq.loading ? (
          <Note>Loading…</Note>
        ) : eqVals.length < 2 ? (
          <Note>No trades to chart for the latest run.</Note>
        ) : (
          <Sparkline values={eqVals} />
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
