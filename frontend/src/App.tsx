import { Rail } from "./components/Rail";
import { TopBar } from "./components/TopBar";
import { ToastProvider } from "./components/Toast";
import { useHealth } from "./hooks/useHealth";
import { Link, RouterProvider, useRouter } from "./lib/router";
import { RefreshProvider } from "./state/refresh";
import { Landing } from "./views/Landing";
import { viewForPath, type ViewDef } from "./views/registry";

function Deck({ view }: { view: ViewDef }) {
  const health = useHealth();
  const Body = view.Component;

  return (
    <div className="deck">
      <a className="skip-link" href="#deck-main">
        Skip to content
      </a>
      <Rail active={view} health={health} />
      <div className="deck__main">
        <TopBar title={view.title} sub={view.sub} />
        <main className="deck__body" id="deck-main">
          {health === "offline" && (
            <div className="banner banner--warn" role="status">
              <i className="ti ti-plug-connected-x banner__icon" aria-hidden="true" />
              <div className="banner__body">
                <strong>API not reachable — showing demo data.</strong>
                <div className="banner__note">
                  Every figure below is generated sample data on the{" "}
                  <code>TEST.*</code> convention, not your journal. Start the API with{" "}
                  <code>python -m api</code> from the repo root, then Reload.
                </div>
              </div>
            </div>
          )}
          {/* Keyed so a route change replays the entrance rather than swapping
              content underneath a static frame. */}
          <div className="deck__view route-fade" key={view.key}>
            <Body />
          </div>
        </main>
      </div>
    </div>
  );
}

function NotFound() {
  return (
    <main className="lp">
      <div className="lp__wrap" style={{ paddingTop: "var(--sp-10)", paddingBottom: "var(--sp-10)" }}>
        <p className="eyebrow">404</p>
        <h1 className="sec__title" style={{ marginBottom: "var(--sp-5)" }}>
          No such page.
        </h1>
        <div style={{ display: "flex", gap: "var(--sp-3)" }}>
          <Link className="btn btn--primary" to="/">
            Back to the front
          </Link>
          <Link className="btn" to="/app">
            Open the panel
          </Link>
        </div>
      </div>
    </main>
  );
}

function Routes() {
  const { path } = useRouter();
  if (path === "/") return <Landing />;
  const view = viewForPath(path);
  // An unrecognised path under /app is a 404, not the Overview.
  if (view) return <Deck view={view} />;
  return <NotFound />;
}

export function App() {
  return (
    <RouterProvider>
      <RefreshProvider>
        <ToastProvider>
          <Routes />
        </ToastProvider>
      </RefreshProvider>
    </RouterProvider>
  );
}
