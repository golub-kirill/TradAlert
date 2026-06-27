import { useEffect, useRef, useState } from "react";

// Native <input type="date"> shows the browser-locale format and can't be
// format-forced, so this field displays/edits as dd/MM/YYYY while emitting an
// ISO yyyy-MM-dd value (what the API expects). The calendar button opens the
// native picker where the browser supports showPicker().

function isoToDisplay(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  return m ? `${m[3]}/${m[2]}/${m[1]}` : "";
}

function displayToIso(s: string): string | null {
  const m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s.trim());
  if (!m) return null;
  const d = +m[1];
  const mo = +m[2];
  const y = +m[3];
  const dt = new Date(y, mo - 1, d);
  // reject impossible dates (e.g. 31/02/2026 rolling over)
  if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
  return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

export function DateField({
  value,
  onChange,
  id,
}: {
  value: string; // ISO yyyy-MM-dd
  onChange: (iso: string) => void;
  id?: string;
}) {
  const [text, setText] = useState(() => isoToDisplay(value));
  const pickerRef = useRef<HTMLInputElement>(null);

  // Keep the display in sync when the ISO value changes from outside.
  useEffect(() => setText(isoToDisplay(value)), [value]);

  const onText = (e: React.ChangeEvent<HTMLInputElement>) => {
    setText(e.target.value);
    const iso = displayToIso(e.target.value);
    if (iso) onChange(iso);
  };

  const openPicker = () => {
    try {
      pickerRef.current?.showPicker();
    } catch {
      /* unsupported / not user-activated — typing still works */
    }
  };

  const invalid = text.trim() !== "" && displayToIso(text) === null;

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, position: "relative" }}>
      <input
        id={id}
        type="text"
        inputMode="numeric"
        placeholder="dd/mm/yyyy"
        value={text}
        onChange={onText}
        style={{ width: 96, borderColor: invalid ? "var(--text-danger)" : undefined }}
      />
      <button type="button" className="ico" title="Pick date" onClick={openPicker}>
        <i className="ti ti-calendar" />
      </button>
      <input
        ref={pickerRef}
        type="date"
        value={value}
        onChange={(e: React.ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
        tabIndex={-1}
        aria-hidden="true"
        style={{
          position: "absolute",
          right: 0,
          bottom: 0,
          width: 1,
          height: 1,
          opacity: 0,
          padding: 0,
          border: 0,
          pointerEvents: "none",
        }}
      />
    </span>
  );
}
