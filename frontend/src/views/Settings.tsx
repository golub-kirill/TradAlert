import { useEffect, useRef, useState } from "react";
import { ApiError, getConfig, getToken, saveConfig, setToken } from "../api/client";
import { Card } from "../components/Card";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { useRefresh } from "../state/refresh";

// Survives a view switch: leaving Settings is not acknowledgement of a stale
// regression baseline. Session-scoped on purpose — it describes this session's
// writes, and data/config_audit.jsonl is the durable record.
//
// sessionStorage is PER TAB, so a second tab neither sees the warning nor the
// dismissal. Accepted for a single-operator loopback panel; the audit log is the
// cross-tab source of truth. Move to localStorage + a `storage` listener if the
// panel ever gets multi-tab or multi-user use.
const PENDING_CHECK_KEY = "tradalert.pendingRegressionCheck";

function readPendingCheck(): string[] {
  try {
    const raw = sessionStorage.getItem(PENDING_CHECK_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

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
  span: 5 | 7;
  rows: Row[];
}

const SECTIONS: Section[] = [
  {
    title: "Scan filters",
    icon: "ti-adjustments",
    span: 7,
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
    span: 5,
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
    span: 5,
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
  // Edge-defining keys written by the last SUCCESSFUL save. Not a toast: that
  // self-dismisses in 2.6s, and "the regression baseline no longer describes the
  // live config" must survive until acknowledged — including a trip to another
  // view, hence sessionStorage rather than plain component state.
  const [needsCheck, setNeedsCheck] = useState<string[]>(readPendingCheck);
  const bannerRef = useRef<HTMLDivElement | null>(null);

  function ackCheck() {
    setNeedsCheck([]);
    try {
      sessionStorage.removeItem(PENDING_CHECK_KEY);
    } catch {
      /* private mode / storage disabled — the banner still clears for this view */
    }
  }

  // Save sits in a savebar stuck to the bottom of a scrolling container, so a
  // banner rendered at the top can land outside the viewport the user is looking
  // at. Always pull it into view; only STEAL FOCUS on the save that raised it —
  // grabbing focus on every later visit would hijack a user who came to Settings
  // to edit something and already knows about the warning.
  const focusBanner = useRef(false);
  useEffect(() => {
    if (!needsCheck.length) return;
    bannerRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (focusBanner.current) {
      bannerRef.current?.focus();
      focusBanner.current = false;
    }
  }, [needsCheck]);
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
      const res = await saveConfig(updates);
      toast(`Saved ${Object.keys(updates).length} change${Object.keys(updates).length > 1 ? "s" : ""}`);
      const edge = res.requires_regression_check ?? [];
      focusBanner.current = edge.length > 0;   // this save raised it — take focus
      setNeedsCheck(edge);
      try {
        if (edge.length) sessionStorage.setItem(PENDING_CHECK_KEY, JSON.stringify(edge));
        else sessionStorage.removeItem(PENDING_CHECK_KEY);
      } catch {
        /* storage unavailable — the in-view banner still works */
      }
      cfg.reload();
      refresh();
    } catch (e) {
      toast(e instanceof ApiError || e instanceof Error ? e.message : String(e), "error");
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

  if (cfg.error)
    return (
      <div className="banner banner--neg" role="alert">
        <i className="ti ti-alert-triangle banner__icon" aria-hidden="true" />
        <div className="banner__body">
          <strong>Config unavailable.</strong>
          <div className="banner__note">
            {cfg.error}
          </div>
        </div>
      </div>
    );

  return (
    <>
      {needsCheck.length > 0 ? (
        <div className="banner banner--warn" role="alert" ref={bannerRef} tabIndex={-1}>
          <i className="ti ti-alert-triangle banner__icon" aria-hidden="true" />
          <div className="banner__body">
            <strong>
              Edge-defining {needsCheck.length === 1 ? "parameter" : "parameters"} changed by
              the last saved edit — the regression baseline no longer describes the live config.
            </strong>
            <div className="banner__note">
              {needsCheck.join(", ")}
            </div>
            <div className="banner__note">
              These change which trades the strategy takes. Re-run{" "}
              <code>python scripts/paired_ab.py</code> before trusting the shipped headline,
              and expect live behaviour to diverge from the backtest until you do. The change
              is journaled to <code>data/config_audit.jsonl</code>.
            </div>
          </div>
          <button className="btn" onClick={ackCheck}>
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="bento">
        {SECTIONS.map((sec) => (
          <Card key={sec.title} title={sec.title} icon={sec.icon} span={sec.span} spot>
            {sec.rows.map(([label, key]) => (
              <div className="setrow" key={key}>
                <span className="setrow__label">{label}</span>
                {control(key)}
              </div>
            ))}
          </Card>
        ))}

        <Card title="Access" icon="ti-key" span={7}>
          <div className="setrow">
            <label className="setrow__label" htmlFor="api-token">
              API token
              <span className="setrow__hint">
                Only needed if the server requires a token for changes.
              </span>
            </label>
            <input
              id="api-token"
              type="password"
              value={tokenVal}
              placeholder="optional"
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setTokenVal(e.target.value)}
            />
          </div>
          <button
            className="btn"
            style={{ marginTop: "var(--sp-4)" }}
            onClick={() => {
              setToken(tokenVal.trim());
              toast("API token saved");
            }}
          >
            <i className="ti ti-device-floppy" aria-hidden="true" />
            Save token
          </button>
        </Card>
      </div>

      {pending > 0 ? (
        <div className="savebar">
          <span className="mut" style={{ fontSize: "var(--fs-data)" }}>
            {pending} unsaved change{pending > 1 ? "s" : ""}
          </span>
          <span style={{ display: "flex", gap: "var(--sp-2)" }}>
            <button className="btn" onClick={() => setEdits(seed())} disabled={saving}>
              Reset
            </button>
            <button
              className="btn btn--primary"
              onClick={onSave}
              disabled={saving}
              aria-busy={saving || undefined}
            >
              <i className="ti ti-device-floppy" aria-hidden="true" />
              {saving ? "Saving…" : "Save changes"}
            </button>
          </span>
        </div>
      ) : null}
    </>
  );
}
