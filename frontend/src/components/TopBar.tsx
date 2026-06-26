import { getScannerLatest } from "../api/client";
import { useApi } from "../hooks/useApi";
import { useRefresh } from "../state/refresh";

function RegimePill() {
  const { data } = useApi(getScannerLatest, []);
  const regime = data?.run?.market_regime || "no regime";
  return (
    <span className="pill">
      <span className="dot" />
      {regime}
    </span>
  );
}

export function TopBar({ title, sub }: { title: string; sub: string }) {
  const { refresh } = useRefresh();
  return (
    <div className="top">
      <div>
        <h3>{title}</h3>
        <div className="sub">{sub}</div>
      </div>
      <div className="act">
        <RegimePill />
        <button className="btn pri" onClick={refresh}>
          <i className="ti ti-refresh"></i>Refresh
        </button>
      </div>
    </div>
  );
}
