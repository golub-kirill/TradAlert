import type { ReactNode } from "react";

export interface KpiItem {
  label: string;
  value: ReactNode;
  tone?: "" | "pos" | "neg" | "warn";
}

export function Kpis({ items }: { items: KpiItem[] }) {
  return (
    <div className="kpis">
      {items.map((k, i) => (
        <div className="kpi" key={i}>
          <div className="l">{k.label}</div>
          <div className={"v " + (k.tone || "")}>{k.value}</div>
        </div>
      ))}
    </div>
  );
}
