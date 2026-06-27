import { useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { TopBar } from "./components/TopBar";
import { ToastProvider } from "./components/Toast";
import { useHealth } from "./hooks/useHealth";
import { RefreshProvider } from "./state/refresh";
import { VIEWS, type ViewKey } from "./views/registry";

function Shell() {
  const [active, setActive] = useState<ViewKey>("overview");
  const health = useHealth();
  const view = VIEWS.find((v) => v.key === active)!;
  const Body = view.Component;

  return (
    <div className="app">
      <Sidebar active={active} onSelect={setActive} health={health} />
      <div className="main">
        <TopBar title={view.title} sub={view.sub} />
        <div className="body">
          {health === "offline" && (
            <div className="banner">
              <i className="ti ti-plug-connected-x"></i>
              <span>
                API not reachable. Start it with <code>python -m api</code> from the repo root, then
                Refresh.
              </span>
            </div>
          )}
          <Body />
        </div>
      </div>
    </div>
  );
}

export function App() {
  return (
    <RefreshProvider>
      <ToastProvider>
        <Shell />
      </ToastProvider>
    </RefreshProvider>
  );
}
