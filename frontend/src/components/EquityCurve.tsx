/* The signature moment: a cumulative-R curve that strokes itself on mount.
 *
 * Rendered as real SVG elements (not an innerHTML string) so React owns the
 * tree and nothing untrusted can reach the DOM. The draw-on is a CSS
 * dashoffset animation — no JS runs per frame.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { cssVars, hasFinePointer, rafThrottle } from "../lib/motion";

export interface CurvePoint {
  label: string;
  value: number;
}

const VB_W = 1000;

/** Sum of segment lengths in viewBox units — the dash length only needs to be
 *  ≥ the true path length for the reveal to look right. */
function polylineLength(pts: Array<[number, number]>): number {
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    total += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  }
  return Math.ceil(total * 1.05);
}

export function EquityCurve({
  points,
  height = 220,
  variant = "panel",
  interactive = false,
  strokeWidth = 2,
}: {
  points: CurvePoint[];
  height?: number;
  /** "hero" stretches to its container and drops the axis furniture. */
  variant?: "panel" | "hero";
  interactive?: boolean;
  strokeWidth?: number;
}) {
  const gid = useId().replace(/:/g, "");
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  const geo = useMemo(() => {
    if (points.length < 2) return null;
    const VB_H = variant === "hero" ? 300 : height;
    const padT = variant === "hero" ? 10 : 12;
    const padB = variant === "hero" ? 0 : 22;
    const padL = 0;
    const padR = variant === "hero" ? 0 : 4;

    const values = points.map((p) => p.value);
    const lo = Math.min(0, ...values);
    const hi = Math.max(...values);
    const pad = (hi - lo) * 0.08 || 1;
    const yLo = lo - pad;
    const yHi = hi + pad;

    const X = (i: number) => padL + (i * (VB_W - padL - padR)) / (points.length - 1);
    const Y = (v: number) => padT + (1 - (v - yLo) / (yHi - yLo || 1)) * (VB_H - padT - padB);

    const xy = points.map((p, i) => [X(i), Y(p.value)] as [number, number]);
    const line = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
    // Only meaningful when zero is inside the plotted domain. For a run that
    // never rose above water, Y(0) lands above the viewBox and the baseline
    // would be silently clipped — better to omit it than to draw it nowhere.
    const base = yLo <= 0 && yHi >= 0 ? Y(0) : null;
    const area = `${line}L${xy[xy.length - 1][0].toFixed(1)},${(VB_H).toFixed(1)}L${xy[0][0].toFixed(1)},${(VB_H).toFixed(1)}Z`;

    return { VB_H, xy, line, area, base, len: polylineLength(xy), yLo, yHi };
  }, [points, height, variant]);

  const onMove = useMemo(
    () =>
      rafThrottle((clientX: number) => {
        const el = wrapRef.current;
        if (!el || !geo) return;
        const r = el.getBoundingClientRect();
        const ratio = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
        setHover(Math.round(ratio * (points.length - 1)));
      }),
    [geo, points.length],
  );

  // Drop any queued frame when the throttle is replaced or the chart unmounts —
  // otherwise it fires against stale geometry and points the crosshair at the
  // wrong bar. Matches the cleanup the other pointer hooks already do.
  useEffect(() => onMove.cancel, [onMove]);

  if (!geo) {
    return (
      <p className="mut" style={{ fontSize: "var(--fs-data)" }}>
        Not enough journaled trades to chart a curve yet.
      </p>
    );
  }

  const hoverPoint = hover != null ? points[hover] : null;
  const hoverX = hover != null ? geo.xy[hover][0] : 0;

  return (
    <div
      className="chartwrap"
      ref={wrapRef}
      onPointerMove={interactive && hasFinePointer() ? (e) => onMove(e.clientX) : undefined}
      onPointerLeave={interactive ? () => setHover(null) : undefined}
    >
      <svg
        viewBox={`0 0 ${VB_W} ${geo.VB_H}`}
        preserveAspectRatio={variant === "hero" ? "none" : "xMidYMid meet"}
        width="100%"
        height={variant === "hero" ? "100%" : undefined}
        role="img"
        aria-label={`Cumulative R equity curve across ${points.length} periods, ending at ${points[points.length - 1].value.toFixed(2)}R.`}
      >
        <defs>
          <linearGradient id={`fill-${gid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--series-1)" stopOpacity="0.22" />
            <stop offset="100%" stopColor="var(--series-1)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {variant === "panel" && geo.base != null && (
          <line
            x1="0"
            x2={VB_W}
            y1={geo.base}
            y2={geo.base}
            stroke="var(--border-strong)"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        )}

        <path className="curve__area" d={geo.area} fill={`url(#fill-${gid})`} />
        <path
          className="curve__line"
          d={geo.line}
          fill="none"
          stroke="var(--series-1)"
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeLinejoin="round"
          /* Keeps the stroke even when the hero variant scales non-uniformly. */
          vectorEffect="non-scaling-stroke"
          style={cssVars({ "--len": geo.len })}
        />

        {hover != null && (
          <>
            <line
              x1={hoverX}
              x2={hoverX}
              y1="0"
              y2={geo.VB_H}
              stroke="var(--text-muted)"
              strokeWidth="1"
              strokeOpacity="0.6"
              vectorEffect="non-scaling-stroke"
            />
            <circle
              cx={hoverX}
              cy={geo.xy[hover][1]}
              r="3"
              fill="var(--surface-0)"
              stroke="var(--series-1)"
              strokeWidth="2"
              vectorEffect="non-scaling-stroke"
            />
          </>
        )}
      </svg>

      {hoverPoint && (
        <div
          className="tip on"
          style={{
            left: `clamp(0px, ${(hoverX / VB_W) * 100}% - 48px, calc(100% - 108px))`,
          }}
        >
          {hoverPoint.label} · {hoverPoint.value >= 0 ? "+" : ""}
          {hoverPoint.value.toFixed(2)}R
        </div>
      )}
    </div>
  );
}
