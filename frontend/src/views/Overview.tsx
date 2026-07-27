import { useMemo } from "react";
import { Card, Empty } from "../components/Card";
import { EquityCurve, type CurvePoint } from "../components/EquityCurve";
import { CountUp, StatStrip } from "../components/Stat";
import { SkeletonBlock, SkeletonStats } from "../components/Skeleton";
import { MonthlyBars } from "../components/PerformanceChart";
import { useApi } from "../hooks/useApi";
import { useSpotlight } from "../hooks/useMotion";
import { Link } from "../lib/router";
import { getBacktests, getMonthly, getPositions, getScannerLatest } from "../api/client";
import { fnum, pct, rstr, signClass } from "../lib/format";

const MISMATCH_SHOWN = 3;

// Mirrors backtest.db.reference_caveat: say when the list is truncated, so three
// shown keys are never mistaken for three total.
function mismatchText(keys: string[] | undefined): string {
  const list = keys ?? [];
  if (!list.length) return "";
  const head = list.slice(0, MISMATCH_SHOWN).join("; ");
  return list.length > MISMATCH_SHOWN
    ? `${head}; … and ${list.length - MISMATCH_SHOWN} more`
    : head;
}

export function Overview() {
  const heroRef = useSpotlight<HTMLElement>();
  // Fetch several, not one: the newest journaled run is often an A/B leg, and
  // headlining it puts an experiment arm's numbers on the dashboard as though
  // they described the strategy. (Run 34 — a VIX-slope-gated leg — showed 44.32R
  // here next to the baseline's 90.83R, which reads as a collapse in the edge.)
  // Five covers any realistic A/B streak without shipping unused param diffs.
  const runs = useApi(() => getBacktests(5), []);
  const positions = useApi(getPositions, []);
  const scan = useApi(getScannerLatest, []);

  const all = runs.data ?? [];
  const newest = all[0];
  // Newest run that measured the shipped config. `null` means "no snapshot to
  // check" and stays eligible — refusing those would leave no baseline at all.
  const r = all.find((x) => x.config_match !== false) ?? newest;
  const latestId = r?.id;
  // Warn when we stepped over a run to find a baseline, AND when there was no
  // baseline to find — otherwise a run of nothing but A/B legs headlines an
  // experiment arm in silence, which is the failure this exists to prevent.
  const skipped = newest && r && newest.id !== r.id ? newest : undefined;
  const unmatched = r?.config_match === false ? r : undefined;

  const monthly = useApi(
    () => (latestId ? getMonthly(latestId) : Promise.resolve(null)),
    [latestId],
  );

  const curve: CurvePoint[] = useMemo(
    () => (monthly.data?.months ?? []).map((m) => ({ label: m.month, value: m.close })),
    [monthly.data],
  );

  const pos = positions.data ?? [];
  const run = scan.data?.run;
  const totalR = r?.total_r ?? null;
  const openR = pos.reduce((s, p) => s + (p.unrealized_r ?? 0), 0);

  // The run's date window, however it was recorded: _meta carries `window` for
  // most runs, older rows only have the two dates. Resolved once rather than
  // mixing ?? and && inside the JSX, where an empty string slipped through as a
  // truthy guard and rendered an empty value.
  const windowLabel =
    r?.window ||
    (r?.start_date && r?.end_date ? `${r.start_date} → ${r.end_date}` : null);

  return (
    <>
      {unmatched ? (
        <div className="banner banner--warn" role="status">
          <i className="ti ti-alert-triangle banner__icon" aria-hidden="true" />
          <div className="banner__body">
            <strong>No matching baseline — these numbers came from a run with a different config.</strong>
            <div className="banner__note">
              Run #{unmatched.id} ran a different config from the shipped <code>filters.yaml</code>,
              and no recent run matched it, so the figures below describe a different strategy.
              Re-journal a full-window baseline. {mismatchText(unmatched.config_mismatch)}
            </div>
          </div>
        </div>
      ) : skipped ? (
        <div className="banner banner--warn" role="status">
          <i className="ti ti-flask banner__icon" aria-hidden="true" />
          <div className="banner__body">
            <strong>Showing run #{latestId} — the newest baseline, not the newest run.</strong>
            <div className="banner__note">
              Run #{skipped.id} ({fnum(skipped.total_r, 2)}R) ran a different config from the shipped{" "}
              <code>filters.yaml</code>, so it did not measure the strategy that ships now — either
              an experiment arm, or a run that predates a config change.{" "}
              {mismatchText(skipped.config_mismatch)}
            </div>
          </div>
        </div>
      ) : null}

      {/* One figure leads; everything else supports it (DESIGN.md §5). */}
      <section className="hero-band card--spot" ref={heroRef}>
        <div className="hero-band__figure">
          <div className="hero-band__eyebrow eyebrow">
            <span>Net cumulative</span>
          </div>
          <div
            className={
              "hero-band__value " + (totalR == null ? "" : totalR >= 0 ? "pos" : "neg")
            }
          >
            {runs.loading && totalR == null ? (
              <span className="skel skel--num" style={{ display: "inline-block", height: 48, width: 200 }} />
            ) : (
              <>
                <CountUp value={totalR} format={(v) => (v >= 0 ? "+" : "") + v.toFixed(2)} />
                <span className="hero-band__unit">R</span>
              </>
            )}
          </div>
          {/* Provenance sits with the figure it describes — never buried. */}
          <div className="hero-band__prov">
            {latestId ? <span>run <b>#{latestId}</b></span> : null}
            {windowLabel ? (
              <span>
                window <b>{windowLabel}</b>
              </span>
            ) : null}
            <span>
              trades <b>{r?.trades_count ?? "—"}</b>
            </span>
            <span>
              config{" "}
              <b className={r?.config_match === false ? "warn" : undefined}>
                {r?.config_match === false ? "mismatch" : r?.config_match ? "matches shipped" : "unverified"}
              </b>
            </span>
          </div>
        </div>

        <div className="hero-band__chart">
          {monthly.loading ? (
            <SkeletonBlock height={200} />
          ) : curve.length >= 2 ? (
            <EquityCurve points={curve} height={200} interactive />
          ) : (
            <Empty icon="ti-chart-line">
              No journaled trades to chart for this run yet.
            </Empty>
          )}
        </div>
      </section>

      <div className="bento">
        <Card title="Run detail" icon="ti-report-analytics" span={7} spot>
          {runs.loading ? (
            <SkeletonStats count={5} />
          ) : (
            <StatStrip
              items={[
                { label: "Win rate", value: pct(r?.win_rate, 1) },
                { label: "Profit factor", value: fnum(r?.profit_factor, 2) },
                { label: "Expectancy", value: fnum(r?.expectancy_r, 3), hint: "R per trade" },
                {
                  label: "Max drawdown",
                  value: fnum(r?.max_drawdown_r, 1),
                  tone: "warn",
                  hint: "peak to trough, R",
                },
                {
                  label: "Up months",
                  value: monthly.data ? pct(monthly.data.up_month_pct) : "—",
                },
              ]}
            />
          )}
        </Card>

        <Card
          title="Latest scan"
          icon="ti-radar"
          span={5}
          spot
          right={
            <Link className="btn btn--sm" to="/app/scanner">
              Open
              <i className="ti ti-arrow-right" aria-hidden="true" />
            </Link>
          }
        >
          {scan.loading ? (
            <SkeletonStats count={3} />
          ) : !run ? (
            <Empty
              icon="ti-radar-off"
              action={
                <Link className="btn btn--sm" to="/app/scanner">
                  Run a scan
                </Link>
              }
            >
              No scans journaled yet.
            </Empty>
          ) : (
            <>
              <StatStrip
                items={[
                  { label: "Scanned", value: run.tickers_scanned },
                  { label: "Passed", value: run.scan_passed },
                  {
                    label: "Fired",
                    value: run.signals_fired,
                    tone: run.signals_fired ? "pos" : "",
                  },
                ]}
              />
              <div className="kv" style={{ marginTop: "var(--sp-4)" }}>
                <span className="kv__k">Run</span>
                <span className="kv__v">#{run.run_id}</span>
              </div>
              <div className="kv">
                <span className="kv__k">Regime</span>
                <span className="kv__v">{run.market_regime ?? "—"}</span>
              </div>
            </>
          )}
        </Card>

        <Card
          title="Open positions"
          icon="ti-briefcase"
          span={5}
          spot
          right={
            <span className={"num " + signClass(openR)} style={{ fontSize: "var(--fs-data)" }}>
              {pos.length ? rstr(openR) : ""}
            </span>
          }
        >
          {positions.loading ? (
            <SkeletonStats count={2} />
          ) : pos.length === 0 ? (
            <Empty
              icon="ti-briefcase-off"
              action={
                <Link className="btn btn--sm" to="/app/positions">
                  Open one
                </Link>
              }
            >
              Nothing held right now.
            </Empty>
          ) : (
            pos.slice(0, 6).map((p) => (
              <div className="kv" key={p.id}>
                <span>
                  <span className="num">{p.ticker}</span>{" "}
                  <span className="mut" style={{ fontSize: "var(--fs-micro)" }}>
                    {p.side}
                  </span>
                </span>
                <span className={"kv__v " + signClass(p.unrealized_r)}>{rstr(p.unrealized_r)}</span>
              </div>
            ))
          )}
        </Card>

        <Card title="Monthly distribution" icon="ti-chart-histogram" span={7}>
          {monthly.loading ? (
            <SkeletonBlock height={200} />
          ) : !monthly.data || monthly.data.months.length === 0 ? (
            <Empty icon="ti-chart-bar-off">No trades to chart for the latest run.</Empty>
          ) : (
            <MonthlyBars months={monthly.data.months} />
          )}
        </Card>
      </div>
    </>
  );
}
