import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  getBacktests,
  getBacktestTrades,
  getConfig,
  runBacktest,
  streamJob,
} from "../api/client";
import type { BacktestMode, BacktestRun, BacktestRunReq } from "../api/types";
import { Card, Empty } from "../components/Card";
import { DateField } from "../components/DateField";
import { Field, Slider, ToggleRow } from "../components/Field";
import { SkeletonText } from "../components/Skeleton";
import { useApi } from "../hooks/useApi";
import { useToast } from "../components/Toast";
import { fnum, pct, rstr, signClass, today } from "../lib/format";

function fiveYearsAgo(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 5);
  return d.toISOString().slice(0, 10);
}

// Container segments that carry no meaning on their own, dropped from the head
// of a dotted key so the label starts at the part that identifies the setting.
const GENERIC_ROOT = new Set(["filters", "settings", "signals"]);

/** Full dotted path (minus a generic root) rather than just the leaf.
 *
 *  The leaf alone is not identifying: filters.yaml has five params ending in
 *  `enabled` and six ending in `min_hist_delta_atr`, so a run that changed the
 *  sector gate and a run that changed PEAD both rendered "ENABLED ON (DEF OFF)",
 *  and a run that changed two of them rendered the same chip twice. */
function paramLabel(k: string): string {
  const parts = k.split(".");
  while (parts.length > 1 && GENERIC_ROOT.has(parts[0])) parts.shift();
  return parts.join(" · ").replaceAll("_", " ");
}
function fmtVal(v: unknown): string {
  if (typeof v === "boolean") return v ? "on" : "off";
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}

/* The four feature switches are ENABLE-ONLY overrides, not state.
 *
 * backtest/run_backtest.py ORs each CLI flag with its filters.yaml key
 * (`if args.chronic_penalty or _chronic_yaml.get("enabled")`), and there is no
 * negative form of any of them. So a run can force a feature on, but nothing
 * the panel sends can turn one off. Rendering them as plain switches defaulted
 * to false claimed otherwise: chronic_loser_penalty and require_trigger_bar_up
 * both ship true, so the UI showed "off" for two features every run enabled —
 * which is how a run logged "Chronic-loser penalty: ENABLED" with the switch
 * visibly off.
 *
 * Seeded from the live config, and locked on when the config already enables
 * them, so the control can never disagree with what the engine will do.
 */
const FEATURE_KEYS = {
  shorts: ["signals", "allow_shorts"],
  chronic: ["chronic_loser_penalty", "enabled"],
  vixSlope: ["regime", "vix_slope_block"],
  antiGap: ["signals", "require_trigger_bar_up"],
} as const;

type FeatureKey = keyof typeof FEATURE_KEYS;
type FeatureFlags = Record<FeatureKey, boolean>;

const NO_FEATURES: FeatureFlags = {
  shorts: false,
  chronic: false,
  vixSlope: false,
  antiGap: false,
};

const LOCKED_HINT =
  "Already on in filters.yaml. The engine ORs this flag with the config, so a single run can turn it on but not off — change it in Settings.";

function readFeatures(filters: Record<string, unknown> | undefined): FeatureFlags {
  const out = { ...NO_FEATURES };
  for (const [name, [section, leaf]] of Object.entries(FEATURE_KEYS)) {
    const s = (filters?.[section] ?? {}) as Record<string, unknown>;
    out[name as FeatureKey] = s[leaf] === true;
  }
  return out;
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
  // Current switch positions, and which of them the shipped config already
  // forces on (those are locked — the engine has no flag to turn them off).
  const [features, setFeatures] = useState<FeatureFlags>(NO_FEATURES);
  const [lockedOn, setLockedOn] = useState<FeatureFlags>(NO_FEATURES);
  const setFeature = (k: FeatureKey) => (v: boolean) =>
    setFeatures((f) => ({ ...f, [k]: v }));

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
        // Feature switches follow the live config rather than a literal, so the
        // control always shows what the run will actually do.
        const shipped = readFeatures(cfg.filters as Record<string, unknown>);
        setLockedOn(shipped);
        setFeatures(shipped);
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
      allow_shorts: features.shorts,
      chronic_penalty: features.chronic,
      vix_slope_gate: features.vixSlope,
      anti_gap_entry: features.antiGap,
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
      setLog(
        "Failed: " +
          (err instanceof ApiError || err instanceof Error ? err.message : String(err)),
      );
      setRunning(false);
    }
  }

  const runs = runsState.data || [];
  const openRunObj = runs.find((r) => r.id === openRun) || null;

  return (
    <>
      <div className="bento">
        <Card title="Window and mode" icon="ti-calendar-stats" span={5} spot>
          <div className="formrow">
            <Field label="From">{() => <DateField value={from} onChange={setFrom} />}</Field>
            <Field label="To">{() => <DateField value={to} onChange={setTo} />}</Field>
            <Field label="Mode">
              {(id) => (
                <select
                  id={id}
                  value={mode}
                  onChange={(e) => setMode(e.target.value as BacktestMode)}
                >
                  <option value="baseline">Baseline</option>
                  <option value="sweep">Parameter sweep</option>
                  <option value="walk-forward">Walk-forward</option>
                  <option value="robustness">Robustness</option>
                </select>
              )}
            </Field>
          </div>

          <button
            className="btn btn--primary btn--lg"
            onClick={onRun}
            disabled={running}
            aria-busy={running || undefined}
            style={{ marginTop: "var(--sp-4)", width: "100%" }}
          >
            <i className={running ? "ti ti-loader-2" : "ti ti-player-play"} aria-hidden="true" />
            {running ? "Running…" : "Run backtest"}
          </button>

          {log ? (
            <pre className="log" style={{ marginTop: "var(--sp-4)" }}>
              {log}
            </pre>
          ) : null}
        </Card>

        <Card title="Parameters" icon="ti-adjustments" span={7} spot>
          <Slider
            label="Open-risk budget"
            value={risk}
            min={1}
            max={10}
            step={0.5}
            onChange={setRisk}
            format={(v) => v.toFixed(1)}
          />
          <Slider
            label="Max hold (days)"
            value={hold}
            min={5}
            max={40}
            step={1}
            onChange={setHold}
            format={(v) => String(Math.round(v))}
          />
          <div className="setrow">
            <label className="setrow__label" htmlFor="holdmode">
              Max-hold mode
            </label>
            <select
              id="holdmode"
              style={{ width: 260 }}
              value={holdMode}
              onChange={(e) => setHoldMode(e.target.value as "if_not_profit" | "hard")}
            >
              <option value="if_not_profit">If not in profit (let winners run)</option>
              <option value="hard">Hard (always exit at cap)</option>
            </select>
          </div>

          <ToggleRow label="Breakeven stop" on={beOn} set={setBeOn} />
          {beOn && (
            <Slider
              label="Breakeven trigger"
              value={be}
              min={0.25}
              max={2}
              step={0.25}
              onChange={setBe}
              format={(v) => v.toFixed(2) + "R"}
            />
          )}
          <ToggleRow label="ATR trailing stop" on={trailOn} set={setTrailOn} />
          {trailOn && (
            <Slider
              label="Trail ATR ×"
              value={trail}
              min={1}
              max={6}
              step={0.5}
              onChange={setTrail}
              format={(v) => v.toFixed(1) + "×"}
            />
          )}
          {(
            [
              ["shorts", "Allow short entries"],
              ["chronic", "Chronic-loser penalty"],
              ["vixSlope", "VIX-slope gate"],
              ["antiGap", "Anti-gap entry"],
            ] as Array<[FeatureKey, string]>
          ).map(([key, label]) => (
            <ToggleRow
              key={key}
              label={label}
              on={features[key]}
              set={setFeature(key)}
              disabled={lockedOn[key]}
              hint={lockedOn[key] ? LOCKED_HINT : undefined}
            />
          ))}
        </Card>
      </div>

      <Card title="Recent runs" icon="ti-history">
        {runsState.loading ? (
          <SkeletonText lines={5} />
        ) : runs.length === 0 ? (
          <Empty icon="ti-flask-off">No backtest runs journaled yet. Run one above.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Window</th>
                  <th>Params</th>
                  <th>Config</th>
                  <th data-num>Trades</th>
                  <th data-num>Total R</th>
                  <th data-num>PF</th>
                  <th data-num>Win</th>
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
                      <td className="mut">
                        {r.window || (r.start_date || "all") + " → " + (r.end_date || "all")}
                      </td>
                      <td className={nCustom ? "" : "mut"}>
                        {nCustom ? `${nCustom} custom` : "default"}
                      </td>
                      <td>
                        {r.config_match === false ? (
                          <span className="tag tag--warn">A/B leg</span>
                        ) : r.config_match ? (
                          <span className="mut">matches</span>
                        ) : (
                          <span className="mut">—</span>
                        )}
                      </td>
                      <td data-num>{r.trades_count}</td>
                      <td data-num className={signClass(r.total_r)}>
                        {fnum(r.total_r, 2)}
                      </td>
                      <td data-num>{fnum(r.profit_factor, 2)}</td>
                      <td data-num>{pct(r.win_rate)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
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
          <i className="ti ti-x" aria-hidden="true" />
          Close
        </button>
      }
    >
      <div style={{ marginBottom: "var(--sp-4)" }}>
        <p className="eyebrow" style={{ marginBottom: "var(--sp-2)" }}>
          Parameters vs default
        </p>
        {run.window || params.length ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--sp-2)" }}>
            {run.window ? <span className="tag tag--param">window · {run.window}</span> : null}
            {params.map((p) => (
              /* title carries the raw dotted key — the label is readable, the
                 tooltip is exact. */
              <span className="tag tag--param" key={p.key} title={p.key}>
                {paramLabel(p.key)}{" "}
                <b className="tag__val">{fmtVal(p.value)}</b>{" "}
                <span className="mut">(def {fmtVal(p.default)})</span>
              </span>
            ))}
          </div>
        ) : (
          <p className="mut" style={{ fontSize: "var(--fs-data)" }}>
            All parameters at the shipped default.
          </p>
        )}
      </div>

      {t.loading ? (
        <SkeletonText lines={5} />
      ) : trades.length === 0 ? (
        <Empty icon="ti-list-search">No trades for this run.</Empty>
      ) : (
        // Up to 200 rows — height-capped so the page stays navigable and the
        // sticky header has a scroll container to stick to.
        <div className="table-wrap table-wrap--scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Dir</th>
                <th>Type</th>
                <th>Entry → Exit</th>
                <th>Reason</th>
                <th data-num>R</th>
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
                    <td data-num className={signClass(r)}>
                      {rstr(r)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
