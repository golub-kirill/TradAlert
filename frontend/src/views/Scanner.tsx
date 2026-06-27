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
          fired.map((f, i) => {
            const isExit = (f.signal_kind || "").startsWith("exit");
            const isHeld = held.has(f.ticker);
            const busy = acting === f.ticker;
            return (
              <div className="sig" key={i}>
                <div className="h">
                  <span className="tk">
                    {f.ticker} ·{" "}
                    {isExit
                      ? (f.signal_kind === "exit_short" ? "cover" : "exit") +
                        (f.signal_type ? ` (${f.signal_type})` : "")
                      : (f.signal_type || "").replaceAll("_", " ") || f.signal_kind}
                  </span>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                    {isExit ? (
                      <span className="badge b-rev">
                        {f.signal_kind === "exit_short" ? "Cover" : "Exit"}
                      </span>
                    ) : (
                      <span className={"badge " + (f.tier === "NEEDS_REVIEW" ? "b-rev" : "b-ok")}>
                        {f.tier === "NEEDS_REVIEW" ? "Needs review" : "Live"}
                      </span>
                    )}
                    {isExit ? (
                      isHeld ? (
                        <button className="btn" disabled={busy} onClick={() => onClose(f)}>
                          <i className="ti ti-x" />
                          Close
                        </button>
                      ) : (
                        <span className="mut" style={{ fontSize: 11 }}>
                          flat
                        </span>
                      )
                    ) : isHeld ? (
                      <span className="mut" style={{ fontSize: 11 }}>
                        held
                      </span>
                    ) : (
                      <button
                        className="btn pri"
                        disabled={busy || f.close == null}
                        onClick={() => onOpen(f)}
                      >
                        <i className="ti ti-plus" />
                        Open
                      </button>
                    )}
                  </span>
                </div>
                <div className="m">
                  {isExit
                    ? f.reason || "exit signal"
                    : `close ${fnum(f.close, 2)} · stop ${fnum(f.stop_price, 2)} · target ${fnum(
                        f.target_price,
                        2,
                      )}${f.review_reason ? " · " + f.review_reason : ""}`}
                </div>
              </div>
            );
          })
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
