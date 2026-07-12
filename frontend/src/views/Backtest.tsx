import {useEffect, useRef, useState} from "react";
import {ApiError, getBacktests, getBacktestTrades, getConfig, runBacktest, streamJob} from "../api/client";
import type {BacktestMode, BacktestRun, BacktestRunReq} from "../api/types";
import {Card, Note} from "../components/Card";
import {DateField} from "../components/DateField";
import {useApi} from "../hooks/useApi";
import {useToast} from "../components/Toast";
import {fnum, pct, rstr, signClass, today} from "../lib/format";

function fiveYearsAgo(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 5);
  return d.toISOString().slice(0, 10);
}

function shortKey(k: string): string {
  return k.split(".").slice(-1)[0].replaceAll("_", " ");
}
function fmtVal(v: unknown): string {
  if (typeof v === "boolean") return v ? "on" : "off";
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}

function Toggle({
  label,
  on,
  set,
}: {
  label: string;
  on: boolean;
  set: (v: boolean) => void;
}) {
  return (
    <div className="setrow">
      <span className="lbl">{label}</span>
      <input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} />
    </div>
  );
}

export function Backtest() {
  const toast = useToast();
  const runsState = useApi(() => getBacktests(12), []);
  const reloadRuns = runsState.reload;

  const [from, setFrom] = useState<string>(fiveYearsAgo());
  const [to, setTo] = useState<string>(today());
  const [mode, setMode] = useState<BacktestMode>("baseline");
  const [risk, setRisk] = useState(5);
  const [hold, setHold] = useState(25);
  const [holdMode, setHoldMode] = useState<"if_not_profit" | "hard">("if_not_profit");
  const [beOn, setBeOn] = useState(true);
  const [be, setBe] = useState(1);
  const [trailOn, setTrailOn] = useState(false);
  const [trail, setTrail] = useState(3);
  const [shorts, setShorts] = useState(false);
  const [chronic, setChronic] = useState(false);
  const [vixSlope, setVixSlope] = useState(false);
  const [antiGap, setAntiGap] = useState(false);

  const [log, setLog] = useState("");
  const [running, setRunning] = useState(false);
  const [openRun, setOpenRun] = useState<number | null>(null);

  const stopRef = useRef<(() => void) | null>(null);
  useEffect(() => () => stopRef.current?.(), []);

    // Seed the form defaults from the LIVE shipped config (GET /config) so the sliders
    // can't silently drift from filters.yaml / settings.yaml — the literals above are
    // only the fallback if the fetch fails. Applied once, on mount.
    const defaultsApplied = useRef(false);
    useEffect(() => {
        getConfig()
            .then((cfg) => {
                if (defaultsApplied.current) return;
                defaultsApplied.current = true;
                const ex = (cfg.filters?.execution ?? {}) as Record<string, unknown>;
                const rk = (cfg.settings?.risk ?? {}) as Record<string, unknown>;
                const n = (v: unknown, d: number) => (typeof v === "number" ? v : d);
                setRisk(n(rk.max_open_risk, 5));
                setHold(n(ex.max_hold_days, 25));
                if (ex.max_hold_mode === "hard" || ex.max_hold_mode === "if_not_profit")
                    setHoldMode(ex.max_hold_mode);
                const beTrig = n(ex.breakeven_trigger_r, 1);
                setBeOn(beTrig > 0);
                if (beTrig > 0) setBe(beTrig);
            })
            .catch(() => {
                /* keep the shipped-literal fallbacks above */
            });
    }, []);

  async function onRun() {
    const req: BacktestRunReq = {
      start: from,
      end: to,
      mode,
      max_open_risk: risk,
      max_hold_days: hold,
      max_hold_mode: holdMode,
      breakeven_trigger_r: beOn ? be : 0,
      allow_shorts: shorts,
      chronic_penalty: chronic,
      vix_slope_gate: vixSlope,
      anti_gap_entry: antiGap,
      ...(trailOn ? { trail_atr_mult: trail } : {}),
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
      setLog("Failed: " + (err instanceof ApiError || err instanceof Error ? err.message : String(err)));
      setRunning(false);
    }
  }

  const runs = runsState.data || [];
  const openRunObj = runs.find((r) => r.id === openRun) || null;

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
            <select value={mode} onChange={(e) => setMode(e.target.value as BacktestMode)}>
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
            onChange={(e) => setRisk(Number(e.target.value))}
          />
          <span className="out">{risk.toFixed(1)}</span>
        </div>
        <div className="ctrl">
          <label>Max hold (days)</label>
          <input
            type="range"
            min={5}
            max={40}
            step={1}
            value={hold}
            onChange={(e) => setHold(Number(e.target.value))}
          />
          <span className="out">{Math.round(hold)}</span>
        </div>
        <div className="ctrl">
          <label>Max-hold mode</label>
          <select
            value={holdMode}
            onChange={(e) => setHoldMode(e.target.value as "if_not_profit" | "hard")}
            style={{ flex: 1 }}
          >
            <option value="if_not_profit">If not in profit (let winners run)</option>
            <option value="hard">Hard (always exit at cap)</option>
          </select>
        </div>

        <Toggle label="Breakeven stop" on={beOn} set={setBeOn} />
        {beOn && (
          <div className="ctrl">
            <label>Breakeven trigger</label>
            <input
              type="range"
              min={0.25}
              max={2}
              step={0.25}
              value={be}
              onChange={(e) => setBe(Number(e.target.value))}
            />
            <span className="out">{be.toFixed(2)}R</span>
          </div>
        )}
        <Toggle label="ATR trailing stop" on={trailOn} set={setTrailOn} />
        {trailOn && (
          <div className="ctrl">
            <label>Trail ATR ×</label>
            <input
              type="range"
              min={1}
              max={6}
              step={0.5}
              value={trail}
              onChange={(e) => setTrail(Number(e.target.value))}
            />
            <span className="out">{trail.toFixed(1)}×</span>
          </div>
        )}
        <Toggle label="Allow short entries" on={shorts} set={setShorts} />
        <Toggle label="Chronic-loser penalty" on={chronic} set={setChronic} />
        <Toggle label="VIX-slope gate" on={vixSlope} set={setVixSlope} />
        <Toggle label="Anti-gap entry" on={antiGap} set={setAntiGap} />

        <button className="btn pri" onClick={onRun} disabled={running} style={{ marginTop: 14 }}>
          <i className={running ? "ti ti-loader-2 spin" : "ti ti-player-play"} />
          {running ? "Running…" : "Run backtest"}
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
                <th>Params</th>
                <th>Trades</th>
                <th>Total R</th>
                <th>PF</th>
                <th>Win</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const nCustom = (r.params?.length ?? 0) + (r.window ? 1 : 0);
                return (
                  <tr
                    key={r.id}
                    onClick={() => setOpenRun(openRun === r.id ? null : r.id)}
                    style={{ cursor: "pointer" }}
                    title="Show details"
                  >
                    <td>
                      {openRun === r.id ? "▸ " : ""}
                      {r.id}
                    </td>
                    <td className="mut">{r.window || (r.start_date || "all") + " → " + (r.end_date || "all")}</td>
                    <td className={nCustom ? "" : "mut"}>{nCustom ? `${nCustom} custom` : "default"}</td>
                    <td>{r.trades_count}</td>
                    <td className={signClass(r.total_r)}>{fnum(r.total_r, 2)}</td>
                    <td>{fnum(r.profit_factor, 2)}</td>
                    <td>{pct(r.win_rate)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>

      {openRunObj && <RunDetail run={openRunObj} onClose={() => setOpenRun(null)} />}
    </>
  );
}

// Expanded run: non-default parameters + the per-run trade list.
function RunDetail({ run, onClose }: { run: BacktestRun; onClose: () => void }) {
  const t = useApi(() => getBacktestTrades(run.id, 200), [run.id]);
  const trades = t.data ?? [];
  const params = run.params ?? [];

  return (
    <Card
      title={`Run #${run.id} · details`}
      icon="ti-list-details"
      right={
        <button className="btn" onClick={onClose}>
          <i className="ti ti-x" />
          Close
        </button>
      }
    >
      <div style={{ marginBottom: 14 }}>
        <div className="mut" style={{ fontSize: 11, marginBottom: 7, letterSpacing: "0.04em", textTransform: "uppercase" }}>
          Parameters vs default
        </div>
        {run.window || params.length ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {run.window ? <span className="stag">window · {run.window}</span> : null}
            {params.map((p) => (
              <span className="stag" key={p.key}>
                {shortKey(p.key)} {fmtVal(p.value)}{" "}
                <span className="mut">(def {fmtVal(p.default)})</span>
              </span>
            ))}
          </div>
        ) : (
          <Note>All parameters at the shipped default.</Note>
        )}
      </div>

      {t.loading ? (
        <Note>Loading trades…</Note>
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
