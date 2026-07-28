/* Shaped placeholders that match the geometry of what is loading. The panel
 * never renders the word "Loading…" (WEB-DESIGN.md §4.11). */

export function SkeletonText({ lines = 3, width = "100%" }: { lines?: number; width?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--sp-3)" }} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="skel skel--text"
          // Ragged right edge reads as text rather than as a broken table.
          style={{ width: i === lines - 1 ? "62%" : width }}
        />
      ))}
    </div>
  );
}

export function SkeletonStats({ count = 4 }: { count?: number }) {
  return (
    <div className="statstrip" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i}>
          <div className="skel skel--text" style={{ width: 64, height: 9 }} />
          <div className="skel skel--num" style={{ marginTop: "var(--sp-2)" }} />
        </div>
      ))}
    </div>
  );
}

export function SkeletonBlock({ height = 180 }: { height?: number }) {
  return <div className="skel skel--block" style={{ height }} aria-hidden="true" />;
}
