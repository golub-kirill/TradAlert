import type { ReactNode } from "react";

export function Card({
  title,
  icon,
  right,
  children,
}: {
  title?: ReactNode;
  icon?: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="card">
      {title != null && (
        <h4>
          {icon && <i className={"ti " + icon}></i>}
          {title}
          {right && <span style={{ marginLeft: "auto", fontWeight: 400 }}>{right}</span>}
        </h4>
      )}
      {children}
    </div>
  );
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="note">{children}</p>;
}
