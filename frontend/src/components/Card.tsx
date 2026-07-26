import type { ReactNode } from "react";
import { useSpotlight } from "../hooks/useMotion";

export function Card({
  title,
  icon,
  right,
  children,
  span,
  spot = false,
  className = "",
}: {
  title?: ReactNode;
  icon?: string;
  right?: ReactNode;
  children: ReactNode;
  /** Bento column span. Omit outside a .bento grid. */
  span?: 4 | 5 | 6 | 7 | 8 | 12;
  /** Pointer-tracked highlight — for tiles the eye should land on. */
  spot?: boolean;
  className?: string;
}) {
  const ref = useSpotlight<HTMLDivElement>();
  const cls = [
    "card",
    spot ? "card--spot" : "",
    span ? `span-${span}` : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <section className={cls} ref={spot ? ref : undefined}>
      {title != null && (
        <header className="card__head">
          <h2 className="card__title">
            {icon && <i className={"ti " + icon} aria-hidden="true" />}
            {title}
          </h2>
          {right}
        </header>
      )}
      <div className="card__body">{children}</div>
    </section>
  );
}

/** Empty state: say what is missing, and name the action that would fill it. */
export function Empty({
  icon = "ti-circle-dashed",
  children,
  action,
}: {
  icon?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <i className={"ti " + icon + " empty__icon"} aria-hidden="true" />
      <span>{children}</span>
      {action}
    </div>
  );
}
