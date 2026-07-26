import { useEffect, useRef, useState } from "react";
import { useApi } from "../hooks/useApi";
import { useTheme } from "../hooks/useTheme";
import { getScannerLatest } from "../api/client";
import { useRefresh } from "../state/refresh";

/** Regime as an instrument reading, not a decorative pill. The dot fires a
 *  single ring when the value actually changes — never a standing loop. */
function Regime() {
  const { data } = useApi(getScannerLatest, []);
  const regime = data?.run?.market_regime ?? null;
  const prev = useRef<string | null>(null);
  const [pulse, setPulse] = useState(false);

  useEffect(() => {
    if (regime && prev.current && prev.current !== regime) {
      setPulse(true);
      const t = window.setTimeout(() => setPulse(false), 750);
      return () => window.clearTimeout(t);
    }
    prev.current = regime;
  }, [regime]);

  return (
    <div className="regime" title="Market regime from the latest journaled scan">
      <span className={"dot" + (pulse ? " dot--pulse" : "") + (regime ? "" : " dot--idle")} aria-hidden="true" />
      <span className="regime__label">Regime</span>
      <span className="regime__value">{regime ?? "—"}</span>
    </div>
  );
}

export function TopBar({ title, sub }: { title: string; sub: string }) {
  const { refresh } = useRefresh();
  const { theme, toggle } = useTheme();

  return (
    <header className="topbar">
      <div>
        <h1 className="topbar__title">{title}</h1>
        <p className="topbar__sub">{sub}</p>
      </div>
      <div className="topbar__act">
        <Regime />
        <button
          className="btn btn--icon"
          onClick={toggle}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          <i className={"ti " + (theme === "dark" ? "ti-sun" : "ti-moon")} aria-hidden="true" />
        </button>
        <button
          className="btn btn--primary"
          onClick={refresh}
          title="Re-pull the latest journaled data (positions, scans, backtests). Does not run a new scan — use the Scanner to re-scan."
        >
          <i className="ti ti-refresh" aria-hidden="true" />
          Reload
        </button>
      </div>
    </header>
  );
}
