// Lightweight filled-area line, used for the Overview equity/price preview.
export function Sparkline({ values, height = 152 }: { values: number[]; height?: number }) {
  const vals = values.filter((v) => v != null && !Number.isNaN(v));
  if (vals.length < 2) return <p className="note">Not enough data for a preview.</p>;
  const W = 620,
    L = 6,
    R = 584,
    Tp = 8,
    B = height - 12;
  const mn = Math.min(...vals),
    mx = Math.max(...vals);
  const X = (i: number) => L + i * ((R - L) / (vals.length - 1));
  const Y = (v: number) => Tp + (1 - (v - mn) / (mx - mn || 1)) * (B - Tp);
  const grid = [0, 1, 2, 3, 4].map((k) => {
    const yy = Tp + k * ((B - Tp) / 4);
    return <line key={k} x1={L} x2={R} y1={yy} y2={yy} style={{ stroke: "var(--border)", strokeWidth: 0.5 }} />;
  });
  const pts = vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  const area =
    `M${X(0)},${B} L` +
    vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" L") +
    ` L${X(vals.length - 1)},${B}Z`;
  return (
    <svg viewBox={`0 0 ${W} ${height}`} width="100%">
      <path d={area} style={{ fill: "var(--bg-accent)", opacity: 0.5 }} />
      <polyline points={pts} style={{ fill: "none", stroke: "var(--text-accent)", strokeWidth: 2 }} />
      {grid}
    </svg>
  );
}
