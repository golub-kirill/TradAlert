import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { HealthState } from "../hooks/useHealth";
import { Link, useRouter } from "../lib/router";
import { VIEWS, type ViewDef } from "../views/registry";

const FOOT: Record<HealthState, string> = {
  online: "API connected",
  offline: "demo data",
  connecting: "connecting…",
};

export function Rail({ active, health }: { active: ViewDef; health: HealthState }) {
  const { path } = useRouter();
  const groupRef = useRef<HTMLDivElement | null>(null);
  const [marker, setMarker] = useState<{ top: number; height: number } | null>(null);

  // The active marker is one sliding element rather than a per-item border, so
  // moving between routes reads as continuous.
  const measure = useCallback(() => {
    const group = groupRef.current;
    if (!group) return;
    const el = group.querySelector<HTMLElement>('[aria-current="page"]');
    setMarker(
      el ? { top: el.offsetTop + el.offsetHeight * 0.25, height: el.offsetHeight * 0.5 } : null,
    );
  }, []);

  useLayoutEffect(measure, [measure, path]);

  // Re-measure on reflow, not just on route change: crossing the icon-rail
  // breakpoint or a late webfont swap changes item offsets, and a marker that
  // only recomputes on navigation ends up detached from the highlighted item.
  useEffect(() => {
    const group = groupRef.current;
    if (!group || typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const ro = new ResizeObserver(measure);
    ro.observe(group);
    return () => ro.disconnect();
  }, [measure]);

  return (
    <nav className="rail" aria-label="Control panel">
      <Link className="rail__brand" to="/">
        <span
          className={"dot" + (health === "offline" ? " dot--idle" : "")}
          aria-hidden="true"
        />
        <span>TradAlert</span>
      </Link>

      <div className="rail__group" ref={groupRef}>
        {marker && (
          <span
            className="rail__marker"
            aria-hidden="true"
            style={{ transform: `translateY(${marker.top}px)`, height: marker.height }}
          />
        )}
        {VIEWS.map((v) => (
          <Link
            key={v.key}
            to={v.path}
            className="nav__item"
            aria-current={v.key === active.key ? "page" : undefined}
            title={v.title}
          >
            <i className={"ti " + v.icon} aria-hidden="true" />
            <span>{v.title}</span>
          </Link>
        ))}
      </div>

      <div className="rail__foot">
        <span
          className={
            "dot" + (health === "offline" ? " dot--idle" : health === "connecting" ? " dot--idle" : "")
          }
          aria-hidden="true"
        />
        <span>{FOOT[health]}</span>
      </div>
    </nav>
  );
}
