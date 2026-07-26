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
import { Card, Empty } from "../components/Card";
import { StatStrip } from "../components/Stat";
import { SkeletonStats, SkeletonText } from "../components/Skeleton";
import { SignalCard } from "../components/SignalCard";
import { fnum, today } from "../lib/format";

/** Which inline confirmation is armed, and for what. Replaces window.confirm so
 *  the question is asked in place, in the panel's own voice. */
interface Pending {
  kind: "open" | "close" | "scan";
  signal?: FiredSignal;
  label: string;
}

function humanKey(k: string): string {
  return k.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Stand-down is a shaped summary, not a blob — render it. Raw JSON stays
 *  available behind a disclosure rather than being the whole presentation. */
function StandDown({ data }: { data: unknown }) {
  if (data == null) {
    return <Empty icon="ti-hand-stop">No stand-down summary for this run.</Empty>;
  }
  const rows =
    typeof data === "object" && !Array.isArray(data)
      ? Object.entries(data as Record<string, unknown>).filter(
          ([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean",
        )
      : [];

  return (
    <>
      {rows.length > 0 ? (
        rows.map(([k, v]) => (
          <div className="kv" key={k}>
            <span className="kv__k">{humanKey(k)}</span>
            <span className="kv__v">{String(v)}</span>
          </div>
        ))
      ) : (
        <p className="mut" style={{ fontSize: "var(--fs-data)" }}>
          The summary for this run has no scalar fields — the raw record is below.
        </p>
      )}
      <details className="disclosure" style={{ marginTop: "var(--sp-3)" }}>
        <summary>Raw record</summary>
        <pre className="log">{JSON.stringify(data, null, 2)}</pre>
      </details>
    </>
  );
}

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
  const [pending, setPending] = useState<Pending | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  // Tear down any live stream on unmount.
  useEffect(() => () => stopRef.current?.(), []);

  function refetchAll() {
    sc.reload();
    positions.reload();
    refresh();
  }

  // Journal-only: open a position from an entry signal (records, never trades).
  async function doOpen(f: FiredSignal) {
    if (f.close == null) return;
    const side = f.signal_kind === "entry_short" ? "short" : "long";
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
      toast("Error: " + (e instanceof Error ? e.message : String(e)), "error");
    } finally {
      setActing(null);
    }
  }

  // Journal-only: close the held position an exit signal refers to.
  async function doClose(f: FiredSignal) {
    const pos = held.get(f.ticker);
    if (!pos || f.close == null) return;
    setActing(f.ticker);
    try {
      await closePosition(pos.id, f.close);
      toast(`${f.ticker} closed at ${fnum(f.close, 2)}`);
      refetchAll();
    } catch (e) {
      toast("Error: " + (e instanceof Error ? e.message : String(e)), "error");
    } finally {
      setActing(null);
    }
  }

  async function doScan() {
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

  function confirmPending() {
    const p = pending;
    setPending(null);
    if (!p) return;
    if (p.kind === "scan") void doScan();
    else if (p.kind === "open" && p.signal) void doOpen(p.signal);
    else if (p.kind === "close" && p.signal) void doClose(p.signal);
  }

  /** The prompt itself. Rendered next to whichever control raised it — a
   *  confirmation for a card near the bottom of the grid must not appear in the
   *  scan panel hundreds of pixels up the page, where the user never sees it. */
  function ConfirmBar({ p }: { p: Pending }) {
    return (
      <div className={"actionbar" + (p.kind === "close" ? " actionbar--danger" : "")}>
        <span style={{ fontSize: "var(--fs-data)" }}>{p.label}</span>
        <span className="actionbar__spacer" />
        <button className="btn" onClick={() => setPending(null)}>
          Cancel
        </button>
        <button
          className={"btn " + (p.kind === "close" ? "btn--neg" : "btn--primary")}
          autoFocus
          onClick={confirmPending}
        >
          Confirm
        </button>
      </div>
    );
  }

  return (
    <>
      <Card
        title="Live scan"
        icon="ti-radar"
        spot
        right={
          <button
            className="btn btn--primary"
            onClick={() => setPending({ kind: "scan", label: "Run a live scan now?" })}
            disabled={running}
            aria-busy={running || undefined}
          >
            <i className={running ? "ti ti-loader-2" : "ti ti-player-play"} aria-hidden="true" />
            {running ? "Scanning…" : "Run scan"}
          </button>
        }
      >
        {log ? (
          <pre className="log">{log}</pre>
        ) : (
          <p className="mut" style={{ fontSize: "var(--fs-data)" }}>
            Trigger a fresh scan; results journal and the panels below refresh when it finishes.
          </p>
        )}

        {pending?.kind === "scan" && <ConfirmBar p={pending} />}
      </Card>

      <Card title="This run" icon="ti-target-arrow" spot>
        {sc.loading ? (
          <SkeletonStats count={4} />
        ) : (
          <StatStrip
            items={[
              { label: "Scanned", value: run?.tickers_scanned ?? "—" },
              { label: "Passed", value: run?.scan_passed ?? "—" },
              {
                label: "Fired",
                value: run?.signals_fired ?? "—",
                tone: run?.signals_fired ? "pos" : "",
              },
              { label: "Run", value: run ? `#${run.run_id}` : "—" },
              { label: "Regime", value: run?.market_regime ?? "—" },
            ]}
          />
        )}
      </Card>

      <Card title="Fired signals" icon="ti-bolt">
        {sc.loading ? (
          <SkeletonText lines={3} />
        ) : fired.length ? (
          <div className="sgrid stagger">
            {fired.map((f, i) => (
              <SignalCard
                key={f.ticker + i}
                f={f}
                held={held.has(f.ticker)}
                busy={acting === f.ticker}
                confirm={
                  pending && pending.kind !== "scan" && pending.signal === f ? (
                    <ConfirmBar p={pending} />
                  ) : undefined
                }
                onOpen={(s) =>
                  setPending({
                    kind: "open",
                    signal: s,
                    label: `Open ${s.ticker} ${s.signal_kind === "entry_short" ? "short" : "long"} at ${fnum(s.close, 2)}? This journals the position — it never places an order.`,
                  })
                }
                onClose={(s) =>
                  setPending({
                    kind: "close",
                    signal: s,
                    label: `Close ${s.ticker} at ${fnum(s.close, 2)}? This journals the exit — it never places an order.`,
                  })
                }
              />
            ))}
          </div>
        ) : (
          <Empty icon="ti-bolt-off">
            Nothing fired in the latest scan. That is a result, not a gap — the rejections are
            journaled below.
          </Empty>
        )}
      </Card>

      <div className="bento">
        <Card title="Stand-down" icon="ti-hand-stop" span={5}>
          {sc.loading ? <SkeletonText lines={4} /> : <StandDown data={sd} />}
        </Card>

        <Card title="Recent scans" icon="ti-history" span={7}>
          {recent.loading ? (
            <SkeletonText lines={5} />
          ) : recentRuns.length === 0 ? (
            <Empty icon="ti-history-off">No scans journaled yet.</Empty>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Time</th>
                    <th>Regime</th>
                    <th data-num>Scanned</th>
                    <th data-num>Passed</th>
                    <th data-num>Fired</th>
                  </tr>
                </thead>
                <tbody>
                  {recentRuns.map((s) => (
                    <tr key={s.id}>
                      <td>{s.id}</td>
                      <td className="mut">{(s.created_at || "").replace("T", " ")}</td>
                      <td>{s.market_regime ?? "—"}</td>
                      <td data-num>{s.tickers_scanned}</td>
                      <td data-num>{s.scan_passed}</td>
                      <td data-num className={s.signals_fired ? "pos" : "mut"}>
                        {s.signals_fired}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
