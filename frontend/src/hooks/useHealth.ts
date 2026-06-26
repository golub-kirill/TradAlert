import { useEffect, useState } from "react";
import { getHealth } from "../api/client";
import { useRefresh } from "../state/refresh";

export type HealthState = "connecting" | "online" | "offline";

// Polls /api/health with a few retries so a slow first request / startup race
// doesn't flash "offline" (ported from the single-file page's 4x retry).
export function useHealth(): HealthState {
  const { tick } = useRefresh();
  const [state, setState] = useState<HealthState>("connecting");

  useEffect(() => {
    let cancelled = false;
    setState("connecting");
    (async () => {
      for (let i = 0; i < 4; i++) {
        try {
          await getHealth();
          if (!cancelled) setState("online");
          return;
        } catch {
          await new Promise((r) => setTimeout(r, 400));
        }
      }
      if (!cancelled) setState("offline");
    })();
    return () => {
      cancelled = true;
    };
  }, [tick]);

  return state;
}
