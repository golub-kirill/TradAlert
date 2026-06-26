import { useEffect, useRef, useState } from "react";
import { getScannerLatest, getScannerRuns, runScan, streamJob } from "../api/client";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { useRefresh } from "../state/refresh";
import { Card, Note } from "../components/Card";
import { Kpis } from "../components/Kpi";
import { fnum } from "../lib/format";

export function Scanner() {
  const sc = useApi(getScannerLatest, []);
  const recent = useApi(() => getScannerRuns(15), []);
  const toast = useToast();
  const { refresh } = useRefresh();
  const run = sc.data?.run;
  const fired = sc.data?.fired ?? [];
  const sd = sc.data?.stand_down;
  const recentRuns = recent.data ?? [];

  const [running, setRunning] = useState(false);
  const [log, setLog] = useState("");
  const stopRef = useRef<(() => void) | null>(null);

  // Tear down any live stream on unmount.
  useEffect(() => () => stopRef.current?.(), []);

  async function onScan() {
    if (!window.confirm("Run a live scan now?")) return;
    setRunning(true);
    setLog("Launching…");
    try {
      const { job_id, cmd } = await runScan({});
      setLog("$ " + cmd + "\n");
      stopRef.current = streamJob(
        job_id,
        (line) => setLog((prev) => prev + line + "\n"),
        (status) => {
          if (status !== "running") {
            setRunning(false);
            toast("Scan " + status);
            sc.reload();
            refresh();
          }
        },
      );
    } catch (err) {
      setLog("Failed: " + (err instanceof Error ? err.message : String(err)));
      setRunning(false);
    }
  }

  return (
    <>
      <Card
        title="Live scan"
        icon="ti-radar"
        right={
          <button className="btn pri" onClick={onScan} disabled={running}>
            <i className="ti ti-player-play" />
            {running ? "Scanning…" : "Run scan"}
          </button>
        }
      >
        {log ? (
          <pre>{log}</pre>
        ) : (
          <Note>Trigger a fresh scan; results journal and the panels below refresh when it finishes.</Note>
        )}
      </Card>

      <Kpis
        items={[
          { label: "Scanned", value: run?.tickers_scanned ?? "—" },
          { label: "Passed", value: run?.scan_passed ?? "—" },
          { label: "Fired", value: run?.signals_fired ?? "—" },
          { label: "Run", value: "#" + (run?.run_id ?? "—") },
        ]}
      />

      <Card title="Fired signals" icon="ti-bolt">
        {fired.length ? (
          fired.map((f, i) => (
            <div className="sig" key={i}>
              <div className="h">
                <span className="tk">
                  {f.ticker} · {(f.signal_type || "").replaceAll("_", " ") || f.signal_kind}
                </span>
                <span className={"badge " + (f.tier === "NEEDS_REVIEW" ? "b-rev" : "b-ok")}>
                  {f.tier === "NEEDS_REVIEW" ? "Needs review" : "Live"}
                </span>
              </div>
              <div className="m">
                close {fnum(f.close, 2)} · stop {fnum(f.stop_price, 2)} · target{" "}
                {fnum(f.target_price, 2)}
                {f.review_reason ? " · " + f.review_reason : ""}
              </div>
            </div>
          ))
        ) : (
          <Note>No fired signals in the latest scan.</Note>
        )}
      </Card>

      <Card title="Stand-down" icon="ti-hand-stop">
        {sd ? <pre>{JSON.stringify(sd, null, 2)}</pre> : <Note>No stand-down summary.</Note>}
      </Card>

      <Card title="Recent scans" icon="ti-history">
        {recentRuns.length === 0 ? (
          <Note>No scans journaled yet.</Note>
        ) : (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Time</th>
                <th>Regime</th>
                <th>Scanned</th>
                <th>Passed</th>
                <th>Fired</th>
              </tr>
            </thead>
            <tbody>
              {recentRuns.map((s) => (
                <tr key={s.id}>
                  <td>{s.id}</td>
                  <td className="mut">{(s.created_at || "").replace("T", " ")}</td>
                  <td>{s.market_regime ?? "—"}</td>
                  <td>{s.tickers_scanned}</td>
                  <td>{s.scan_passed}</td>
                  <td className={s.signals_fired ? "pos" : "mut"}>{s.signals_fired}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
