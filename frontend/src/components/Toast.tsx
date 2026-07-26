import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

interface ToastItem {
  id: number;
  msg: string;
  kind: "info" | "error";
}

type ToastFn = (msg: string, kind?: "info" | "error") => void;
const Ctx = createContext<ToastFn>(() => {});

const LIFETIME = 2600;
const MAX = 3;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef<number[]>([]);

  // Clear pending dismissals on unmount — otherwise a toast fired seconds before
  // teardown calls setItems on a torn-down provider.
  useEffect(
    () => () => {
      timers.current.forEach(window.clearTimeout);
      timers.current = [];
    },
    [],
  );

  const toast = useCallback<ToastFn>((msg, kind = "info") => {
    const id = Date.now() + Math.random();
    // Cap the stack so a burst of errors can't paper over the UI.
    setItems((prev) => [...prev, { id, msg, kind }].slice(-MAX));
    const t = window.setTimeout(() => {
      setItems((prev) => prev.filter((x) => x.id !== id));
      timers.current = timers.current.filter((x) => x !== t);
    }, LIFETIME);
    timers.current.push(t);
  }, []);

  return (
    <Ctx.Provider value={toast}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={"toast" + (t.kind === "error" ? " toast--neg" : "")}>
            {t.msg}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);
