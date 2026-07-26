import { useState, type ReactNode } from "react";
import {
  closePosition,
  editPosition,
  getPositions,
  openPosition,
  scaleOut,
  updateStop,
  type OpenBody,
} from "../api/client";
import type { Position } from "../api/types";
import { Card, Empty } from "../components/Card";
import { DateField } from "../components/DateField";
import { Field } from "../components/Field";
import { StatStrip } from "../components/Stat";
import { SkeletonText } from "../components/Skeleton";
import { useToast } from "../components/Toast";
import { useApi } from "../hooks/useApi";
import { fnum, rstr, signClass, tickerOk, today } from "../lib/format";
import { useRefresh } from "../state/refresh";

// Which inline action is open against which row.
type ActionKind = "stop" | "close" | "scale" | "edit";
interface ActionPanel {
  id: number;
  ticker: string;
  kind: ActionKind;
  value: string;
  fraction: number; // for "scale": portion to close
  // Close requires an explicit second click before it fires.
  armed: boolean;
}

const LABEL: Record<ActionKind, string> = {
  stop: "New stop price",
  close: "Exit price",
  scale: "Exit price (partial)",
  edit: "New entry price",
};

const ROW_ACTIONS: Array<{ kind: ActionKind; icon: string; title: string }> = [
  { kind: "stop", icon: "ti-arrow-bar-to-up", title: "Move stop" },
  { kind: "close", icon: "ti-x", title: "Close" },
  { kind: "scale", icon: "ti-arrows-split", title: "Partial close" },
  { kind: "edit", icon: "ti-pencil", title: "Edit entry" },
];

export function Positions() {
  const ps = useApi(getPositions, []);
  const pos: Position[] = ps.data ?? [];
  const toast = useToast();
  const { refresh } = useRefresh();

  const [panel, setPanel] = useState<ActionPanel | null>(null);
  const [busy, setBusy] = useState(false);
  const [showOpen, setShowOpen] = useState(false);

  const totalR = pos.reduce((s, p) => s + (p.unrealized_r ?? 0), 0);
  const longs = pos.filter((p) => p.side === "long").length;
  const shorts = pos.filter((p) => p.side === "short").length;

  function openAction(p: Position, kind: ActionKind) {
    const seed =
      kind === "close" || kind === "scale"
        ? p.current
        : kind === "edit"
          ? p.entry_price
          : p.stop_price;
    setPanel({
      id: p.id,
      ticker: p.ticker,
      kind,
      value: seed != null ? String(seed) : "",
      fraction: 0.5,
      armed: false,
    });
  }

  async function confirmAction() {
    if (!panel) return;
    const v = Number(panel.value);
    if (!Number.isFinite(v) || v <= 0) {
      toast("Enter a price greater than 0.", "error");
      return;
    }
    // Close is destructive: first Confirm arms, second commits.
    if (panel.kind === "close" && !panel.armed) {
      setPanel({ ...panel, armed: true });
      return;
    }
    setBusy(true);
    try {
      if (panel.kind === "stop") {
        await updateStop(panel.id, v);
        toast(`${panel.ticker} stop → ${fnum(v, 2)}`);
      } else if (panel.kind === "close") {
        await closePosition(panel.id, v);
        toast(`${panel.ticker} closed at ${fnum(v, 2)}`);
      } else if (panel.kind === "scale") {
        await scaleOut(panel.id, v, panel.fraction);
        toast(`${panel.ticker} scaled ${Math.round(panel.fraction * 100)}% out at ${fnum(v, 2)}`);
      } else {
        await editPosition(panel.id, { entry_price: v });
        toast(`${panel.ticker} entry → ${fnum(v, 2)}`);
      }
      setPanel(null);
      ps.reload();
      refresh();
    } catch (err) {
      toast("Error: " + (err instanceof Error ? err.message : String(err)), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Card title="Exposure" icon="ti-scale" spot>
        {ps.loading ? (
          <SkeletonText lines={2} />
        ) : (
          <StatStrip
            items={[
              { label: "Held", value: pos.length },
              {
                label: "Unrealized",
                value: pos.length ? rstr(totalR) : "—",
                tone: totalR < 0 ? "neg" : "pos",
              },
              { label: "Long", value: longs },
              { label: "Short", value: shorts },
            ]}
          />
        )}
      </Card>

      <Card
        title="Held positions"
        icon="ti-briefcase"
        right={
          <button
            className="btn btn--primary"
            onClick={() => setShowOpen((s) => !s)}
            aria-expanded={showOpen}
          >
            <i className={showOpen ? "ti ti-x" : "ti ti-plus"} aria-hidden="true" />
            {showOpen ? "Cancel" : "Open position"}
          </button>
        }
      >
        {showOpen && <OpenForm onClose={() => setShowOpen(false)} onDone={() => ps.reload()} />}

        {ps.loading ? (
          <SkeletonText lines={4} />
        ) : pos.length === 0 ? (
          <Empty
            icon="ti-briefcase-off"
            action={
              <button className="btn btn--sm" onClick={() => setShowOpen(true)}>
                Record one
              </button>
            }
          >
            Nothing held right now.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Side</th>
                  <th data-num>Entry</th>
                  <th data-num>Stop</th>
                  <th data-num>Now</th>
                  <th data-num>R</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {pos.map((p) => (
                  <tr key={p.id}>
                    <td>{p.ticker}</td>
                    <td className="mut">{p.side}</td>
                    <td data-num>{fnum(p.entry_price, 2)}</td>
                    <td data-num>{fnum(p.stop_price, 2)}</td>
                    <td data-num>{fnum(p.current, 2)}</td>
                    <td data-num className={signClass(p.unrealized_r)}>
                      {rstr(p.unrealized_r)}
                    </td>
                    <td>
                      <span style={{ display: "flex", gap: "var(--sp-1)" }}>
                        {ROW_ACTIONS.map((a) => (
                          <button
                            key={a.kind}
                            className="btn btn--icon"
                            title={`${a.title} · ${p.ticker}`}
                            aria-label={`${a.title} ${p.ticker}`}
                            onClick={() => openAction(p, a.kind)}
                          >
                            <i className={"ti " + a.icon} aria-hidden="true" />
                          </button>
                        ))}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {panel && (
          <div className={"actionbar" + (panel.kind === "close" ? " actionbar--danger" : "")}>
            <Field label={`${LABEL[panel.kind]} · ${panel.ticker}`}>
              {(id) => (
                <input
                  id={id}
                  type="number"
                  step="0.01"
                  min="0"
                  autoFocus
                  value={panel.value}
                  onChange={(e) => setPanel({ ...panel, value: e.target.value, armed: false })}
                />
              )}
            </Field>

            {panel.kind === "scale" && (
              <Field label="Fraction">
                {(id) => (
                  <select
                    id={id}
                    value={panel.fraction}
                    onChange={(e) => setPanel({ ...panel, fraction: Number(e.target.value) })}
                  >
                    <option value={0.5}>½ (50%)</option>
                    <option value={0.3333}>⅓ (33%)</option>
                    <option value={0.25}>¼ (25%)</option>
                  </select>
                )}
              </Field>
            )}

            <span className="actionbar__spacer" />
            <button className="btn" disabled={busy} onClick={() => setPanel(null)}>
              Cancel
            </button>
            <button
              className={"btn " + (panel.kind === "close" ? "btn--neg" : "btn--primary")}
              disabled={busy}
              aria-busy={busy || undefined}
              onClick={confirmAction}
            >
              {panel.kind === "close"
                ? panel.armed
                  ? "Yes — close it"
                  : "Close position"
                : panel.kind === "scale"
                  ? "Scale out"
                  : "Confirm"}
            </button>
          </div>
        )}
      </Card>

      <p className="mut" style={{ fontSize: "var(--fs-micro)" }}>
        Edits are journal-only — they record to the positions table, never place a real order.
      </p>
    </>
  );
}

// Inline new-position form. Validates ticker + positive entry, then journals it.
function OpenForm({ onClose, onDone }: { onClose: () => void; onDone: () => void }): ReactNode {
  const toast = useToast();
  const { refresh } = useRefresh();
  const [ticker, setTicker] = useState("");
  const [entry, setEntry] = useState("");
  const [side, setSide] = useState("long");
  const [stop, setStop] = useState("");
  const [date, setDate] = useState(today());
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const badTicker = ticker !== "" && !tickerOk(ticker);

  async function submit() {
    if (!tickerOk(ticker)) {
      toast("Enter a valid ticker.", "error");
      return;
    }
    const entryPrice = Number(entry);
    if (!Number.isFinite(entryPrice) || entryPrice <= 0) {
      toast("Entry price must be greater than 0.", "error");
      return;
    }
    const stopPrice = stop.trim() === "" ? null : Number(stop);
    if (stopPrice != null && (!Number.isFinite(stopPrice) || stopPrice <= 0)) {
      toast("Stop price must be greater than 0.", "error");
      return;
    }
    const payload: OpenBody = {
      ticker: ticker.trim(),
      entry_price: entryPrice,
      side,
      stop_price: stopPrice,
      entry_date: date || null,
      notes: notes.trim(),
    };
    setBusy(true);
    try {
      await openPosition(payload);
      toast(`${payload.ticker} opened`);
      setTicker("");
      setEntry("");
      setSide("long");
      setStop("");
      setDate(today());
      setNotes("");
      onClose();
      onDone();
      refresh();
    } catch (err) {
      toast("Error: " + (err instanceof Error ? err.message : String(err)), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="formrow" style={{ marginBottom: "var(--sp-5)" }}>
      <Field label="Ticker" hint={badTicker ? "Letters, digits, dot or dash." : undefined}>
        {(id) => (
          <input
            id={id}
            value={ticker}
            placeholder="TEST.1"
            aria-invalid={badTicker || undefined}
            onChange={(e) => setTicker(e.target.value)}
          />
        )}
      </Field>
      <Field label="Entry price">
        {(id) => (
          <input
            id={id}
            type="number"
            step="0.01"
            min="0"
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
          />
        )}
      </Field>
      <Field label="Side">
        {(id) => (
          <select id={id} value={side} onChange={(e) => setSide(e.target.value)}>
            <option value="long">long</option>
            <option value="short">short</option>
          </select>
        )}
      </Field>
      <Field label="Stop price">
        {(id) => (
          <input
            id={id}
            type="number"
            step="0.01"
            min="0"
            value={stop}
            placeholder="optional"
            onChange={(e) => setStop(e.target.value)}
          />
        )}
      </Field>
      <Field label="Entry date">
        {(id) => <DateField id={id} value={date} onChange={setDate} />}
      </Field>
      <Field label="Notes">
        {(id) => (
          <input
            id={id}
            value={notes}
            placeholder="optional"
            onChange={(e) => setNotes(e.target.value)}
          />
        )}
      </Field>
      <button className="btn btn--primary" disabled={busy} aria-busy={busy || undefined} onClick={submit}>
        <i className="ti ti-check" aria-hidden="true" />
        Add
      </button>
    </div>
  );
}
