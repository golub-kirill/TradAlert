import { useEffect, useRef, useState } from "react";
import { getBacktests, getBacktestTrades, runBacktest, streamJob } from "../api/client";
import { ApiError } from "../api/client";
import type { BacktestMode, BacktestRunReq } from "../api/types";
import { Card, Note } from "../components/Card";
import { DateField } from "../components/DateField";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { fnum, pct, rstr, signClass, today } from "../lib/format";

// today() minus 5 years, as YYYY-MM-DD.
function fiveYearsAgo(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 5);
  return d.toISOString().slice(0, 10);
}

export function Backtest() {
  const toast = useToast();
  const runsState = useApi(() => getBacktests(12), []);
  const reloadRuns = runsState.reload;

  const [from, setFrom] = useState<string>(fiveYearsAgo());
  const [to, setTo] = useState<string>(today());
  const [mode, setMode] = useState<BacktestMode>("baseline");
  const [risk, setRisk] = useState(5);
  const [be, setBe] = useState(1);
  const [hold, setHold] = useState(25);
  const [shorts, setShorts] = useState(false);
  const [log, setLog] = useState("");
  const [running, setRunning] = useState(false);
  const [openRun, setOpenRun] = useState<number | null>(null);

  const stopRef = useRef<(() => void) | null>(null);

  // Tear down any live stream on unmount.
  useEffect(() => {
    return () => {
      if (stopRef.current) stopRef.current();
    };
  }, []);

  async function onRun() {
    const req: BacktestRunReq = {
      start: from,
      end: to,
      mode,
      max_open_risk: risk,
      breakeven_trigger_r: be,
      max_hold_days: hold,
      allow_shorts: shorts,
    };
    setRunning(true);
    setLog("Launching…");
    try {
      const { job_id, cmd } = await runBacktest(req);
      setLog("$ " + cmd + "\n");
      stopRef.current = streamJob(
        job_id,
        (line) => setLog((prev) => prev + line + "\n"),
        (status) => {
          if (status !== "running") {
            setRunning(false);
            toast("Backtest " + status);
            reloadRuns();
          }
        },
      );
    } catch (err) {
      const message = err instanceof ApiError || err instanceof Error ? err.message : String(err);
      setLog("Failed: " + message);
      setRunning(false);
    }
  }

  const runs = runsState.data || [];

  return (
    <>
      <Card title="Run a backtest" icon="ti-flask">
        <div className="dates">
          <div className="fld">
            From
            <DateField value={from} onChange={setFrom} />
          </div>
          <div className="fld">
            To
            <DateField value={to} onChange={setTo} />
          </div>
          <div className="fld">
            Mode
            <select
              value={mode}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                setMode(e.target.value as BacktestMode)
              }
            >
              <option value="baseline">Baseline</option>
              <option value="sweep">Parameter sweep</option>
              <option value="walk-forward">Walk-forward</option>
              <option value="robustness">Robustness</option>
            </select>
          </div>
        </div>

        <div className="ctrl">
          <label>Open-risk budget</label>
          <input
            type="range"
            min={1}
            max={10}
            step={0.5}
            value={risk}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setRisk(Number(e.target.value))}
          />
          <span className="out">{risk.toFixed(1)}</span>
        </div>
        <div className="ctrl">
          <label>Breakeven trigger</label>
          <input
            type="range"
            min={0}
            max={2}
            step={0.25}
            value={be}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setBe(Number(e.target.value))}
          />
          <span className="out">{be.toFixed(2)}R</span>
        </div>
        <div className="ctrl">
          <label>Max hold (days)</label>
          <input
            type="range"
            min={5}
            max={40}
            step={1}
            value={hold}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setHold(Number(e.target.value))}
          />
          <span className="out">{Math.round(hold)}</span>
        </div>

        <label
          style={{
            fontSize: "12.5px",
            color: "var(--text-secondary)",
            display: "flex",
            alignItems: "center",
            gap: "8px",
            marginBottom: "12px",
          }}
        >
          <input
            type="checkbox"
            checked={shorts}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setShorts(e.target.checked)}
          />
          Allow short entries
        </label>

        <button className="btn pri" onClick={onRun} disabled={running}>
          <i className="ti ti-player-play" />
          Run backtest
        </button>

        {log && <pre>{log}</pre>}
      </Card>

      <Card title="Recent runs" icon="ti-history">
        {runs.length === 0 ? (
          <Note>No backtest runs journaled yet. Run one above.</Note>
        ) : (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Window</th>
                <th>Trades</th>
                <th>Total R</th>
                <th>PF</th>
                <th>Win</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setOpenRun(openRun === r.id ? null : r.id)}
                  style={{ cursor: "pointer" }}
                  title="Show trades"
                >
                  <td>{openRun === r.id ? "▸ " : ""}{r.id}</td>
                  <td className="mut">
                    {(r.start_date || "all") + " → " + (r.end_date || "all")}
                  </td>
                  <td>{r.trades_count}</td>
                  <td className={signClass(r.total_r)}>{fnum(r.total_r, 2)}</td>
                  <td>{fnum(r.profit_factor, 2)}</td>
                  <td>{pct(r.win_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {openRun != null && <TradesPanel runId={openRun} onClose={() => setOpenRun(null)} />}
    </>
  );
}

// Per-run trade list, shown when a Recent-runs row is clicked.
function TradesPanel({ runId, onClose }: { runId: number; onClose: () => void }) {
  const t = useApi(() => getBacktestTrades(runId, 200), [runId]);
  const trades = t.data ?? [];
  return (
    <Card
      title={`Trades · run #${runId}`}
      icon="ti-list-details"
      right={
        <button className="btn" onClick={onClose}>
          <i className="ti ti-x" />
          Close
        </button>
      }
    >
      {t.loading ? (
        <Note>Loading…</Note>
      ) : trades.length === 0 ? (
        <Note>No trades for this run.</Note>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Dir</th>
              <th>Type</th>
              <th>Entry → Exit</th>
              <th>Reason</th>
              <th>R</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((tr, i) => {
              const r = tr.effective_r ?? tr.r_multiple;
              return (
                <tr key={i}>
                  <td>{tr.ticker}</td>
                  <td className="mut">{tr.direction}</td>
                  <td className="mut">{(tr.signal_type || "").replaceAll("_", " ")}</td>
                  <td className="mut">{(tr.entry_date || "?") + " → " + (tr.exit_date || "?")}</td>
                  <td className="mut">{tr.exit_reason ?? "—"}</td>
                  <td className={signClass(r)}>{rstr(r)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Card>
  );
}
