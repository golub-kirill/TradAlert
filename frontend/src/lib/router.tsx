/* Minimal history router — ~60 lines instead of a dependency.
 *
 * The panel only needs "what is the current pathname" plus "go there without a
 * reload", so react-router would be four times this file's weight for features
 * nothing here uses (nested outlets, loaders, param matching).
 *
 * Deep links require the server to fall back to index.html for unknown paths —
 * api/main.py does that for any non-/api, non-asset GET.
 */

import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type ReactNode,
} from "react";

const NAV_EVENT = "ta:navigate";

interface RouterValue {
  path: string;
  navigate: (to: string, opts?: { replace?: boolean }) => void;
}

const Ctx = createContext<RouterValue>({ path: "/", navigate: () => {} });

function currentPath(): string {
  const p = window.location.pathname || "/";
  // Normalise a trailing slash so "/app/" and "/app" match the same route.
  return p.length > 1 && p.endsWith("/") ? p.slice(0, -1) : p;
}

export function RouterProvider({ children }: { children: ReactNode }) {
  const [path, setPath] = useState(currentPath);

  useEffect(() => {
    const sync = () => setPath(currentPath());
    window.addEventListener("popstate", sync);
    window.addEventListener(NAV_EVENT, sync);
    return () => {
      window.removeEventListener("popstate", sync);
      window.removeEventListener(NAV_EVENT, sync);
    };
  }, []);

  const navigate = useCallback((to: string, opts?: { replace?: boolean }) => {
    if (to === currentPath()) return;
    window.history[opts?.replace ? "replaceState" : "pushState"]({}, "", to);
    window.dispatchEvent(new Event(NAV_EVENT));
    window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
  }, []);

  const value = useMemo(() => ({ path, navigate }), [path, navigate]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRouter(): RouterValue {
  return useContext(Ctx);
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string };

/** An anchor that navigates in-app but stays a real link (middle-click, copy
 *  link address, and "open in new tab" all keep working). Forwards its ref so
 *  effects like the magnetic CTA can attach to it. */
export const Link = forwardRef<HTMLAnchorElement, LinkProps>(function Link(
  { to, onClick, children, ...rest },
  ref,
) {
  const { navigate } = useRouter();
  return (
    <a
      ref={ref}
      href={to}
      onClick={(e) => {
        onClick?.(e);
        // Let the browser handle modified clicks and anything not left-button.
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        navigate(to);
      }}
      {...rest}
    >
      {children}
    </a>
  );
});
