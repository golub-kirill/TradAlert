/* The L2 effect hooks. Each one degrades to a static end-state under
 * prefers-reduced-motion rather than simply not running — nothing may be left
 * invisible or mid-transform. See WEB-DESIGN.md §7. */

import { useCallback, useEffect, useRef, useState } from "react";
import { easeOutCubic, hasFinePointer, prefersReducedMotion, rafThrottle } from "../lib/motion";

/** Adds `.in` when the element first enters the viewport, then unobserves.
 *  No standing scroll listener. */
export function useReveal<T extends HTMLElement = HTMLDivElement>(rootMargin = "0px 0px -12% 0px") {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (prefersReducedMotion() || typeof IntersectionObserver === "undefined") {
      el.classList.add("in");
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      { rootMargin, threshold: 0.15 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [rootMargin]);

  return ref;
}

/** Counts a numeral up from zero on mount. One rAF loop per value; reduced
 *  motion returns the final number immediately. */
export function useCountUp(target: number | null | undefined, duration = 900): number {
  const [value, setValue] = useState(() => (prefersReducedMotion() ? (target ?? 0) : 0));
  const frame = useRef(0);

  useEffect(() => {
    const end = target ?? 0;
    if (prefersReducedMotion()) {
      setValue(end);
      return;
    }
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      setValue(end * easeOutCubic(t));
      if (t < 1) frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame.current);
  }, [target, duration]);

  return value;
}

/** Pointer-tracked highlight: writes --mx/--my as percentages, one write per
 *  frame. Bound only on fine pointers. */
export function useSpotlight<T extends HTMLElement = HTMLDivElement>() {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !hasFinePointer()) return;
    const move = rafThrottle((x: number, y: number) => {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", `${((x - r.left) / r.width) * 100}%`);
      el.style.setProperty("--my", `${((y - r.top) / r.height) * 100}%`);
    });
    const onMove = (e: PointerEvent) => move(e.clientX, e.clientY);
    el.addEventListener("pointermove", onMove);
    return () => {
      el.removeEventListener("pointermove", onMove);
      move.cancel();
    };
  }, []);

  return ref;
}

/** Magnetic pull toward the cursor, capped at `strength` px. Not bound at all
 *  under reduced motion or on coarse pointers. */
export function useMagnetic<T extends HTMLElement = HTMLButtonElement>(strength = 6, radius = 90) {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || !hasFinePointer() || prefersReducedMotion()) return;

    const move = rafThrottle((x: number, y: number) => {
      const r = el.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const dx = x - cx;
      const dy = y - cy;
      const dist = Math.hypot(dx, dy);
      if (dist > radius + Math.max(r.width, r.height) / 2) {
        el.style.setProperty("--dx", "0px");
        el.style.setProperty("--dy", "0px");
        return;
      }
      const pull = Math.min(1, radius / Math.max(dist, 1)) * strength;
      el.style.setProperty("--dx", `${(dx / Math.max(dist, 1)) * pull}px`);
      el.style.setProperty("--dy", `${(dy / Math.max(dist, 1)) * pull}px`);
    });

    const onMove = (e: PointerEvent) => move(e.clientX, e.clientY);
    const reset = () => {
      el.style.setProperty("--dx", "0px");
      el.style.setProperty("--dy", "0px");
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerleave", reset);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerleave", reset);
      move.cancel();
      reset();
    };
  }, [strength, radius]);

  return ref;
}

/** Six lime particles on click, removed when their animation ends. */
export function useClickSpark() {
  return useCallback((e: React.MouseEvent<HTMLElement>) => {
    if (prefersReducedMotion() || !hasFinePointer()) return;
    const host = e.currentTarget;
    const r = host.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    for (let i = 0; i < 6; i++) {
      const angle = (Math.PI * 2 * i) / 6 + Math.random() * 0.4;
      const dist = 18 + Math.random() * 16;
      const s = document.createElement("span");
      s.className = "spark";
      s.style.left = `${x}px`;
      s.style.top = `${y}px`;
      s.style.setProperty("--sx", `${Math.cos(angle) * dist}px`);
      s.style.setProperty("--sy", `${Math.sin(angle) * dist}px`);
      s.addEventListener("animationend", () => s.remove(), { once: true });
      host.appendChild(s);
    }
  }, []);
}

/** Tracks the document scroll past a threshold — drives the landing nav's
 *  stuck state. Passive listener, removed on unmount. */
export function useScrolled(threshold = 12): boolean {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = rafThrottle(() => setScrolled(window.scrollY > threshold));
    const handler = () => onScroll();
    window.addEventListener("scroll", handler, { passive: true });
    handler();
    return () => {
      window.removeEventListener("scroll", handler);
      onScroll.cancel();
    };
  }, [threshold]);
  return scrolled;
}
