import { useEffect, useState } from "react";
import { getConfig, getToken, saveConfig, setToken, ApiError } from "../api/client";
import { Card, Note } from "../components/Card";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { useRefresh } from "../state/refresh";

// The config payload exposes an `editable` whitelist of dotted keys alongside
// the read-only sections. It is not on ConfigResponse, so read it defensively.
interface ConfigShape {
  filters?: unknown;
  settings?: unknown;
  editable?: string[];
}

// Walk nested config keys; return an em-dash placeholder when anything is missing.
function gv(obj: any, ...keys: string[]): unknown {
  let cur: any = obj;
  for (const k of keys) {
    if (cur == null || typeof cur !== "object") return "—";
    cur = cur[k];
  }
  return cur == null ? "—" : cur;
}

// Render a boolean as on/off; pass other values through.
function onOff(v: unknown): string {
  if (v === true) return "on";
  if (v === false) return "off";
  return String(v);
}

// Friendly labels for known editable keys; falls back to the raw dotted key.
const LABELS: Record<string, string> = {
  "settings.risk.max_open_risk": "Open-risk budget (R)",
  "settings.scanner.event_risk_within_days": "Event-risk window (days)",
  "settings.telegram.enabled": "Telegram alerts",
  "settings.telegram.send_stand_down": 'Send "no signals" message',
};

// Read the current value of a dotted key from the config payload. The first
// segment selects the section (filters vs settings); the rest walks the object.
function readKey(cfg: ConfigShape | null, key: string): unknown {
  if (!cfg) return undefined;
  const parts = key.split(".");
  const head = parts[0];
  const rest = parts.slice(1);
  const root = head === "filters" ? cfg.filters : head === "settings" ? cfg.settings : undefined;
  let cur: any = root;
  for (const k of rest) {
    if (cur == null || typeof cur !== "object") return undefined;
    cur = cur[k];
  }
  return cur;
}

export function Settings() {
  const cfg = useApi(getConfig, []);
  const toast = useToast();
  const { refresh } = useRefresh();

  const data = (cfg.data ?? null) as ConfigShape | null;
  const f = data?.filters ?? {};
  const s = data?.settings ?? {};
  const editable = data?.editable ?? [];

  // Local edits keyed by dotted key; seeded from config when it (re)loads.
  const [edits, setEdits] = useState<Record<string, number | boolean>>({});
  const [saving, setSaving] = useState(false);
  const [tokenVal, setTokenVal] = useState<string>(getToken());

  useEffect(() => {
    if (!data) return;
    const seed: Record<string, number | boolean> = {};
    for (const key of data.editable ?? []) {
      const v = readKey(data, key);
      if (typeof v === "boolean") seed[key] = v;
      else if (typeof v === "number") seed[key] = v;
    }
    setEdits(seed);
  }, [data]);

  const setBool = (key: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setEdits((prev) => ({ ...prev, [key]: e.target.checked }));
  const setNum = (key: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setEdits((prev) => ({ ...prev, [key]: e.target.valueAsNumber }));

  // Collect only keys whose value differs from the current config value.
  function changedUpdates(): Record<string, number | boolean> {
    const out: Record<string, number | boolean> = {};
    for (const key of editable) {
      const cur = readKey(data, key);
      const next = edits[key];
      if (next === undefined) continue;
      if (typeof next === "number" && Number.isNaN(next)) continue;
      if (next !== cur) out[key] = next;
    }
    return out;
  }

  async function onSave() {
    const updates = changedUpdates();
    if (Object.keys(updates).length === 0) {
      toast("Nothing changed");
      return;
    }
    setSaving(true);
    try {
      await saveConfig(updates);
      toast("Saved");
      cfg.reload();
      refresh();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  function onSaveToken() {
    setToken(tokenVal.trim());
    toast("API token saved");
  }

  const filterRows: Array<[string, string]> = [
    ["Min price", String(gv(f, "price", "min_price"))],
    ["Min $ volume 20d", String(gv(f, "liquidity", "min_dollar_volume_20d"))],
    ["ATR% band", gv(f, "volatility", "min_atr_pct") + " – " + gv(f, "volatility", "max_atr_pct")],
    ["MA fast / slow", gv(f, "trend", "ma_fast") + " / " + gv(f, "trend", "ma_slow")],
    ["Min R:R", String(gv(f, "signals", "stop_loss", "min_rr"))],
    ["ATR stop ×", String(gv(f, "signals", "stop_loss", "atr_multiplier"))],
    ["Max hold", String(gv(f, "execution", "max_hold_days"))],
    ["Breakeven trigger", gv(f, "execution", "breakeven_trigger_r") + "R"],
    ["VIX low / high", gv(f, "regime", "vix_low") + " / " + gv(f, "regime", "vix_high")],
  ];

  const layerRows: Array<[string, string]> = [
    ["Macro layer", onOff(gv(s, "macro", "enabled"))],
    ["Behavioral layer", onOff(gv(s, "behavioral", "enabled"))],
    ["Allow shorts", onOff(gv(f, "signals", "allow_shorts"))],
    ["Sector gate", onOff(gv(f, "signals", "sector_gate", "enabled"))],
    ["Open-risk budget", String(gv(s, "risk", "max_open_risk"))],
    ["Event-risk window", gv(s, "scanner", "event_risk_within_days") + "d"],
  ];

  if (cfg.error) return <div className="banner">Config unavailable: {cfg.error}</div>;

  return (
    <>
      <div className="grid2">
        <Card title="Filters" icon="ti-adjustments">
          {filterRows.map(([label, value]) => (
            <div className="row" key={label}>
              <span className="mut">{label}</span>
              <span>{value}</span>
            </div>
          ))}
        </Card>

        <Card title="Layers" icon="ti-server-2">
          {layerRows.map(([label, value]) => (
            <div className="row" key={label}>
              <span className="mut">{label}</span>
              <span>{value}</span>
            </div>
          ))}
        </Card>
      </div>

      <Card title="Editable settings" icon="ti-pencil">
        {editable.length === 0 ? (
          <Note>No editable settings exposed by the server.</Note>
        ) : (
          editable.map((key) => {
            const label = LABELS[key] ?? key;
            const val = edits[key];
            const isBool = typeof readKey(data, key) === "boolean";
            return (
              <div className="ctrl" key={key}>
                <label htmlFor={"set-" + key}>{label}</label>
                {isBool ? (
                  <input
                    id={"set-" + key}
                    type="checkbox"
                    checked={val === true}
                    onChange={setBool(key)}
                  />
                ) : (
                  <input
                    id={"set-" + key}
                    type="number"
                    step="any"
                    value={typeof val === "number" && !Number.isNaN(val) ? val : ""}
                    onChange={setNum(key)}
                  />
                )}
              </div>
            );
          })
        )}
        <button className="btn pri" onClick={onSave} disabled={saving || cfg.loading}>
          <i className="ti ti-device-floppy" />
          {saving ? "Saving…" : "Save"}
        </button>
        <Note>Other parameters are locked here and are changed in the YAML with a regression check.</Note>
      </Card>

      <Card title="API token" icon="ti-key">
        <div className="ctrl">
          <label htmlFor="set-token">API token</label>
          <input
            id="set-token"
            type="password"
            placeholder="token"
            value={tokenVal}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTokenVal(e.target.value)}
          />
          <button className="btn" onClick={onSaveToken}>
            Save
          </button>
        </div>
        <Note>Only needed if the server requires a token for changes.</Note>
      </Card>
    </>
  );
}
