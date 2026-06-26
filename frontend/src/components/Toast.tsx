import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

type ToastFn = (msg: string) => void;
const Ctx = createContext<ToastFn>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState("");
  const [show, setShow] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const toast = useCallback((m: string) => {
    setMsg(m);
    setShow(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setShow(false), 2600);
  }, []);

  return (
    <Ctx.Provider value={toast}>
      {children}
      <div className={"toast" + (show ? " show" : "")}>{msg}</div>
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);
