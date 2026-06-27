import { useEffect, useState } from "react";
import { ApiError, getConfig, getToken, saveConfig, setToken } from "../api/client";
import { Card, Note } from "../components/Card";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { useRefresh } from "../state/refresh";

interface ConfigShape {
  filters?: unknown;
  settings?: unknown;
  editable?: string[];
}

// Read a dotted key (first segment = filters|settings) from the config payload.
function readKey(cfg: ConfigShape | null, key: string): unknown {
  if (!cfg) return undefined;
  const parts = key.split(".");
  const root = parts[0] === "filters" ? cfg.filters : parts[0] === "settings" ? cfg.settings : undefined;
  let cur: unknown = root;
  for (const k of parts.slice(1)) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[k];
  }
  return cur;
}

type Row = [label: string, key: string];
interface Section {
  title: string;
  icon: string;
  rows: Row[];
}

const SECTIONS: Section[] = [
  {
    title: "Scan filters",
    icon: "ti-adjustments",
    rows: [
      ["Min price", "filters.price.min_price"],
      ["Min $ volume 20d", "filters.liquidity.min_dollar_volume_20d"],
      ["Min ATR %", "filters.volatility.min_atr_pct"],
      ["Max ATR %", "filters.volatility.max_atr_pct"],
      ["MA fast", "filters.trend.ma_fast"],
      ["MA slow", "filters.trend.ma_slow"],
      ["Min R:R", "filters.signals.stop_loss.min_rr"],
      ["ATR stop ×", "filters.signals.stop_loss.atr_multiplier"],
      ["Max hold (days)", "filters.execution.max_hold_days"],
      ["Breakeven trigger (R)", "filters.execution.breakeven_trigger_r"],
      ["VIX low", "filters.regime.vix_low"],
      ["VIX high", "filters.regime.vix_high"],
    ],
  },
  {
    title: "Layers & risk",
    icon: "ti-server-2",
    rows: [
      ["Macro layer", "settings.macro.enabled"],
      ["Behavioral layer", "settings.behavioral.enabled"],
      ["Allow shorts", "filters.signals.allow_shorts"],
      ["Sector gate", "filters.signals.sector_gate.enabled"],
      ["Open-risk budget (R)", "settings.risk.max_open_risk"],
      ["Event-risk window (days)", "settings.scanner.event_risk_within_days"],
    ],
  },
  {
    title: "Notifications",
    icon: "ti-bell",
    rows: [
      ["Telegram alerts", "settings.telegram.enabled"],
      ['Send "no signals" message', "settings.telegram.send_stand_down"],
    ],
  },
];

export function Settings() {
  const cfg = useApi(getConfig, []);
  const toast = useToast();
  const { refresh } = useRefresh();

  const data = (cfg.data ?? null) as ConfigShape | null;
  const editable = new Set(data?.editable ?? []);

  const [edits, setEdits] = useState<Record<string, number | boolean>>({});
  const [saving, setSaving] = useState(false);
  const [tokenVal, setTokenVal] = useState<string>(getToken());

  function seed(): Record<string, number | boolean> {
    const s: Record<string, number | boolean> = {};
    for (const key of data?.editable ?? []) {
      const v = readKey(data, key);
      if (typeof v === "boolean" || typeof v === "number") s[key] = v;
    }
    return s;
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setEdits(seed()), [data]);

  function changed(): Record<string, number | boolean> {
    const out: Record<string, number | boolean> = {};
    for (const key of editable) {
      const next = edits[key];
      if (next === undefined) continue;
      if (typeof next === "number" && Number.isNaN(next)) continue;
      if (next !== readKey(data, key)) out[key] = next;
    }
    return out;
  }
  const pending = Object.keys(changed()).length;

  async function onSave() {
    const updates = changed();
    if (!Object.keys(updates).length) return;
    setSaving(true);
    try {
      await saveConfig(updates);
      toast(`Saved ${Object.keys(updates).length} change${Object.keys(updates).length > 1 ? "s" : ""}`);
      cfg.reload();
      refresh();
    } catch (e) {
      toast(e instanceof ApiError || e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  function control(key: string) {
    const cur = readKey(data, key);
    if (!editable.has(key)) return <span className="mut">{cur == null ? "—" : String(cur)}</span>;
    const val = edits[key];
    if (typeof cur === "boolean") {
      return (
        <input
          type="checkbox"
          checked={val === true}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
            setEdits((p) => ({ ...p, [key]: e.target.checked }))
          }
        />
      );
    }
    return (
      <input
        type="number"
        step="any"
        value={typeof val === "number" && !Number.isNaN(val) ? val : ""}
        onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
          setEdits((p) => ({ ...p, [key]: e.target.valueAsNumber }))
        }
      />
    );
  }

  if (cfg.error) return <div className="banner">Config unavailable: {cfg.error}</div>;

  return (
    <>
      <div className="grid2">
        {SECTIONS.map((sec) => (
          <Card key={sec.title} title={sec.title} icon={sec.icon}>
            {sec.rows.map(([label, key]) => (
              <div className="setrow" key={key}>
                <span className="lbl">{label}</span>
                {control(key)}
              </div>
            ))}
          </Card>
        ))}

        <Card title="Access" icon="ti-key">
          <div className="setrow">
            <span className="lbl">API token</span>
            <input
              type="password"
              value={tokenVal}
              placeholder="optional"
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTokenVal(e.target.value)}
            />
          </div>
          <button
            className="btn"
            style={{ marginTop: 12 }}
            onClick={() => {
              setToken(tokenVal.trim());
              toast("API token saved");
            }}
          >
            <i className="ti ti-device-floppy" />
            Save token
          </button>
          <Note>Only needed if the server requires a token for changes.</Note>
        </Card>
      </div>

      {pending > 0 ? (
        <div className="savebar">
          <span className="mut">
            {pending} unsaved change{pending > 1 ? "s" : ""}
          </span>
          <span style={{ display: "flex", gap: 8 }}>
            <button className="btn" onClick={() => setEdits(seed())} disabled={saving}>
              Reset
            </button>
            <button className="btn pri" onClick={onSave} disabled={saving}>
              <i className="ti ti-device-floppy" />
              {saving ? "Saving…" : "Save changes"}
            </button>
          </span>
        </div>
      ) : null}
    </>
  );
}
