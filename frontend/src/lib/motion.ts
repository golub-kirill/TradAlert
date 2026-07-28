/* Shared motion primitives. Every effect built on these must have a complete
 * static path when the user asks for reduced motion — see WEB-DESIGN.md §7. */

import type {CSSProperties} from "react";

/** Custom properties in a style prop. CSSProperties has no index signature, so
 *  the cast is the documented way to pass CSS variables through React. */
export function cssVars(vars: Record<string, string | number>): CSSProperties {
  return vars as CSSProperties;
}

export function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Hover-only affordances (spotlight, magnetic, crosshair-on-hover) bind behind
 *  this so touch devices never get a dead interaction. */
export function hasFinePointer(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(hover: hover) and (pointer: fine)").matches
  );
}

/** Collapse a burst of pointer events into one write per frame. Returns a
 *  cancel fn so callers can detach cleanly on unmount. */
export function rafThrottle<A extends unknown[]>(fn: (...args: A) => void) {
  let frame = 0;
  let last: A | null = null;
  const run = () => {
    frame = 0;
    if (last) fn(...last);
    last = null;
  };
  const wrapped = (...args: A) => {
    last = args;
    if (!frame) frame = requestAnimationFrame(run);
  };
  wrapped.cancel = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    last = null;
  };
  return wrapped;
}

export const easeOutCubic = (t: number): number => 1 - Math.pow(1 - t, 3);
