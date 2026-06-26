import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

// A global "refresh" tick. The top-bar Refresh button bumps it; data hooks fold
// it into their dependency list so every view re-fetches together.
interface RefreshCtx {
  tick: number;
  refresh: () => void;
}

const Ctx = createContext<RefreshCtx>({ tick: 0, refresh: () => {} });

export function RefreshProvider({ children }: { children: ReactNode }) {
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);
  const value = useMemo(() => ({ tick, refresh }), [tick, refresh]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useRefresh = () => useContext(Ctx);
