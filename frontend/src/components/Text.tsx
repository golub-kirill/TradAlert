/* Text-animation primitives. All three split on words (never characters) so
 * screen readers and text selection keep working, and all three resolve to
 * their final state under prefers-reduced-motion via motion.css. */

import type { ReactNode } from "react";
import { useReveal } from "../hooks/useMotion";
import { cssVars } from "../lib/motion";

/** Hero headline: each word rises out of its own clip, staggered.
 *  `accent` marks the single lime word — the brand's only tint on the page. */
export function MaskWords({
  text,
  accent,
  className = "",
  delayStep = 1,
}: {
  text: string;
  accent?: string;
  className?: string;
  /** Multiplier on the per-word stagger, for a second line that should follow on. */
  delayStep?: number;
}) {
  const words = text.split(" ");
  return (
    <span className={className}>
      {words.map((w, i) => (
        <span key={i}>
          <span className="reveal-mask">
            <span style={cssVars({ "--i": i * delayStep })}>
              {accent && w === accent ? <em>{w}</em> : w}
            </span>
          </span>
          {i < words.length - 1 ? " " : null}
        </span>
      ))}
    </span>
  );
}

/** Body copy that cascades from dim to full as it enters view. */
export function CascadeWords({ text, className = "" }: { text: string; className?: string }) {
  const ref = useReveal<HTMLParagraphElement>();
  const words = text.split(" ");
  return (
    <p className={"cascade " + className} ref={ref}>
      {words.map((w, i) => (
        <span key={i} style={cssVars({ "--i": i })}>
          {w}
          {i < words.length - 1 ? " " : null}
        </span>
      ))}
    </p>
  );
}

/** Scroll-triggered blur-to-sharp reveal for section headings and blocks. */
export function Reveal({
  children,
  className = "",
  as: Tag = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "section" | "h2" | "p";
}) {
  const ref = useReveal<HTMLDivElement>();
  return (
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Tag className={"reveal " + className} ref={ref as any}>
      {children}
    </Tag>
  );
}
