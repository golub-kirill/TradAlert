import type { HealthState } from "../hooks/useHealth";
import type { ViewKey } from "../views/registry";
import { VIEWS } from "../views/registry";

export function Sidebar({
  active,
  onSelect,
  health,
}: {
  active: ViewKey;
  onSelect: (v: ViewKey) => void;
  health: HealthState;
}) {
  const foot =
    health === "online" ? "API connected" : health === "offline" ? "API offline" : "connecting…";
  return (
    <aside className="side">
      <div className="brand">
        <span className={"dot" + (health === "offline" ? " off" : "")} />
        TradAlert
      </div>
      {VIEWS.map((v) => (
        <button
          key={v.key}
          className={"nav" + (active === v.key ? " on" : "")}
          onClick={() => onSelect(v.key)}
        >
          <i className={"ti " + v.icon}></i>
          {v.title}
        </button>
      ))}
      <div style={{ marginTop: "auto", fontSize: 11, color: "var(--text-muted)", padding: 9 }}>
        {foot}
      </div>
    </aside>
  );
}
