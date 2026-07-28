import type {ReactNode} from "react";
import {useCountUp} from "../hooks/useMotion";

export type Tone = "" | "pos" | "neg" | "warn";

export interface StatItem {
  label: string;
  value: ReactNode;
  tone?: Tone;
  hint?: ReactNode;
}

export function Stat({ label, value, tone = "", hint }: StatItem) {
  return (
    <div>
      <div className="stat__label">{label}</div>
      <div className={"stat__value" + (tone ? ` stat__value--${tone}` : "")}>{value}</div>
      {hint ? <div className="stat__delta">{hint}</div> : null}
    </div>
  );
}

/** Compact strip of figures inside a bento tile — never a page-wide row of
 *  equal boxes (WEB-DESIGN.md §5). */
export function StatStrip({ items }: { items: StatItem[] }) {
  return (
    <div className="statstrip">
      {items.map((s, i) => (
        <Stat key={i} {...s} />
      ))}
    </div>
  );
}

/** A numeral that counts up on mount. `format` receives the interpolated value
 *  so the caller keeps control of sign, digits and suffix. */
export function CountUp({
  value,
  format,
  duration,
}: {
  value: number | null | undefined;
  format: (v: number) => string;
  duration?: number;
}) {
  const animated = useCountUp(value, duration);
  if (value == null || Number.isNaN(value)) return <>—</>;
  return <>{format(animated)}</>;
}
