/* Light/dark toggle. The explicit choice always wins over the media query, in
 * both directions — light mode is a deliverable (decks, print), not a filter.
 * index.html applies the stored value before first paint to avoid a flash. */

import { useCallback, useEffect, useState } from "react";

export type Theme = "dark" | "light";
const KEY = "ta-theme";

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null; // private mode
  }
}

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => stored() ?? systemTheme());

  // Reflect the current theme, but do NOT persist here: writing on mount would
  // store the system-derived default as though the user had chosen it, and the
  // panel would then stop following later OS theme changes. Only toggle writes.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  // Follow the OS while the user has made no explicit choice.
  useEffect(() => {
    if (stored()) return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const sync = () => setTheme(mq.matches ? "light" : "dark");
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const toggle = useCallback(() => {
    setTheme((t) => {
      const next: Theme = t === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, next);
      } catch {
        /* private mode — the attribute still applies for this session */
      }
      return next;
    });
  }, []);

  return { theme, toggle };
}
