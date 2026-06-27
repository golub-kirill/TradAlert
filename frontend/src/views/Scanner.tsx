import { useEffect, useRef, useState } from "react";
import {
  closePosition,
  getPositions,
  getScannerLatest,
  getScannerRuns,
  openPosition,
  runScan,
  streamJob,
} from "../api/client";
import type { FiredSignal } from "../api/types";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { useRefresh } from "../state/refresh";
import { Card, Note } from "../components/Card";
import { Kpis } from "../components/Kpi";
import { SignalCard } from "../components/SignalCard";
import { fnum, today } from "../lib/format";

export function Scanner() {
  const sc = useApi(getScannerLatest, []);
  const recent = useApi(() => getScannerRuns(15), []);
  const positions = useApi(getPositions, []);
  const toast = useToast();
  const { refresh } = useRefresh();
  const run = sc.data?.run;
  const fired = sc.data?.fired ?? [];
  const sd = sc.data?.stand_down;
  const recentRuns = recent.data ?? [];
  // ticker -> open position, to wire Close on exit signals
  const held = new Map((positions.data ?? []).map((p) => [p.ticker, p] as const));

  const [running, setRunning] = useState(false);
  const [log, setLog] = useState("");
  const [acting, setActing] = useState<string | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  // Tear down any live stream on unmount.
  useEffect(() => () => stopRef.current?.(), []);

  function refetchAll() {
    sc.reload();
    positions.reload();
    refresh();
  }

  // Journal-only: open a position from an entry signal (records, never trades).
  async function onOpen(f: FiredSignal) {
    if (f.close == null) return;
    const side = f.signal_kind === "entry_short" ? "short" : "long";
    if (!window.confirm(`Open ${f.ticker} ${side} at ${fnum(f.close, 2)}? (journal-only)`)) return;
    setActing(f.ticker);
    try {
      await openPosition({
        ticker: f.ticker,
        entry_price: f.close,
        side,
        stop_price: f.stop_price,
        entry_date: today(),
      });
      toast(`${f.ticker} opened ${side}`);
      refetchAll();
    } catch (e) {
      toast("Error: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setActing(null);
    }
  }

  // Journal-only: close the held position an exit signal refers to.
  async function onClose(f: FiredSignal) {
    const pos = held.get(f.ticker);
    if (!pos || f.close == null) return;
    if (!window.confirm(`Close ${f.ticker} at ${fnum(f.close, 2)}? (journal-only)`)) return;
    setActing(f.ticker);
    try {
      await closePosition(pos.id, f.close);
      toast(`${f.ticker} closed at ${fnum(f.close, 2)}`);
      refetchAll();
    } catch (e) {
      toast("Error: " + (e instanceof Error ? e.message : String(e)));
    } finally {
      setActing(null);
    }
  }

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
          <div className="sgrid">
            {fired.map((f, i) => (
              <SignalCard
                key={i}
                f={f}
                held={held.has(f.ticker)}
                busy={acting === f.ticker}
                onOpen={onOpen}
                onClose={onClose}
              />
            ))}
          </div>
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
