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
import { Card, Note } from "../components/Card";
import { DateField } from "../components/DateField";
import { Kpis } from "../components/Kpi";
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
      toast("Enter a price greater than 0.");
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
      toast("Error: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Kpis
        items={[
          { label: "Held", value: pos.length },
          { label: "Unrealized", value: rstr(totalR), tone: totalR < 0 ? "neg" : "pos" },
          { label: "Long", value: longs },
          { label: "Short", value: shorts },
        ]}
      />

      <Card
        title="Held positions"
        icon="ti-briefcase"
        right={
          <button className="btn pri" onClick={() => setShowOpen((s) => !s)}>
            <i className="ti ti-plus" />
            Open position
          </button>
        }
      >
        {showOpen && <OpenForm onClose={() => setShowOpen(false)} onDone={() => ps.reload()} />}

        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Stop</th>
              <th>Now</th>
              <th>R</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {pos.length === 0 ? (
              <tr>
                <td colSpan={7} className="note">
                  No open positions.
                </td>
              </tr>
            ) : (
              pos.map((p) => (
                <tr key={p.id}>
                  <td>{p.ticker}</td>
                  <td className="mut">{p.side}</td>
                  <td>{fnum(p.entry_price, 2)}</td>
                  <td>{fnum(p.stop_price, 2)}</td>
                  <td>{fnum(p.current, 2)}</td>
                  <td className={signClass(p.unrealized_r)}>{rstr(p.unrealized_r)}</td>
                  <td>
                    <span style={{ display: "flex", gap: 5 }}>
                      <button
                        className="ico"
                        title="Move stop"
                        onClick={() => openAction(p, "stop")}
                      >
                        <i className="ti ti-arrow-bar-to-up" />
                      </button>
                      <button className="ico" title="Close" onClick={() => openAction(p, "close")}>
                        <i className="ti ti-x" />
                      </button>
                      <button
                        className="ico"
                        title="Partial close"
                        onClick={() => openAction(p, "scale")}
                      >
                        <i className="ti ti-arrows-split" />
                      </button>
                      <button className="ico" title="Edit" onClick={() => openAction(p, "edit")}>
                        <i className="ti ti-pencil" />
                      </button>
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {panel && (
          <div
            className="card"
            style={{ marginTop: 12, display: "flex", alignItems: "flex-end", gap: 12 }}
          >
            <label className="fld">
              {LABEL[panel.kind]} · {panel.ticker}
              <input
                type="number"
                step="0.01"
                min="0"
                autoFocus
                value={panel.value}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                  setPanel({ ...panel, value: e.target.value, armed: false })
                }
              />
            </label>
            {panel.kind === "scale" && (
              <label className="fld">
                Fraction
                <select
                  value={panel.fraction}
                  onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                    setPanel({ ...panel, fraction: Number(e.target.value) })
                  }
                >
                  <option value={0.5}>½ (50%)</option>
                  <option value={0.3333}>⅓ (33%)</option>
                  <option value={0.25}>¼ (25%)</option>
                </select>
              </label>
            )}
            <button className="btn" disabled={busy} onClick={() => setPanel(null)}>
              Cancel
            </button>
            <button
              className={"btn" + (panel.kind === "close" ? "" : " pri")}
              disabled={busy}
              style={
                panel.kind === "close"
                  ? { borderColor: "var(--border-accent)", color: "var(--text-danger)" }
                  : undefined
              }
              onClick={confirmAction}
            >
              {panel.kind === "close"
                ? panel.armed
                  ? "Confirm close"
                  : "Close position"
                : panel.kind === "scale"
                  ? "Scale out"
                  : "Confirm"}
            </button>
          </div>
        )}
      </Card>

      <Note>
        Edits are journal-only — they record to the positions table, never place a real order.
      </Note>
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

  async function submit() {
    if (!tickerOk(ticker)) {
      toast("Enter a valid ticker.");
      return;
    }
    const entryPrice = Number(entry);
    if (!Number.isFinite(entryPrice) || entryPrice <= 0) {
      toast("Entry price must be greater than 0.");
      return;
    }
    const stopPrice = stop.trim() === "" ? null : Number(stop);
    if (stopPrice != null && (!Number.isFinite(stopPrice) || stopPrice <= 0)) {
      toast("Stop price must be greater than 0.");
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
      toast("Error: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dates" style={{ marginBottom: 14 }}>
      <label className="fld">
        Ticker
        <input
          value={ticker}
          placeholder="TEST.1"
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTicker(e.target.value)}
        />
      </label>
      <label className="fld">
        Entry price
        <input
          type="number"
          step="0.01"
          min="0"
          value={entry}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setEntry(e.target.value)}
        />
      </label>
      <label className="fld">
        Side
        <select
          value={side}
          onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setSide(e.target.value)}
        >
          <option value="long">long</option>
          <option value="short">short</option>
        </select>
      </label>
      <label className="fld">
        Stop price
        <input
          type="number"
          step="0.01"
          min="0"
          value={stop}
          placeholder="optional"
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setStop(e.target.value)}
        />
      </label>
      <label className="fld">
        Entry date
        <DateField value={date} onChange={setDate} />
      </label>
      <label className="fld">
        Notes
        <input
          value={notes}
          placeholder="optional"
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setNotes(e.target.value)}
        />
      </label>
      <button className="btn pri" disabled={busy} onClick={submit}>
        <i className="ti ti-check" />
        Add
      </button>
      <button className="btn" disabled={busy} onClick={onClose}>
        Cancel
      </button>
    </div>
  );
}
