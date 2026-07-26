/* Form primitives shared by Backtest, Positions and Settings. Labels are real
 * <label> elements so clicking one focuses its control. */

import { useId, type ReactNode } from "react";

export function Field({
  label,
  hint,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: (id: string) => ReactNode;
}) {
  const id = useId();
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {children(id)}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </div>
  );
}

export function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  format = (v) => v.toFixed(1),
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  format?: (v: number) => string;
}) {
  const id = useId();
  return (
    <div className="slider">
      <label className="slider__label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <output className="slider__out" htmlFor={id}>
        {format(value)}
      </output>
    </div>
  );
}

export function ToggleRow({
  label,
  hint,
  on,
  set,
  disabled,
}: {
  label: string;
  hint?: ReactNode;
  on: boolean;
  set: (v: boolean) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="setrow">
      <label className="setrow__label" htmlFor={id}>
        {label}
        {hint ? <span className="setrow__hint">{hint}</span> : null}
      </label>
      <input
        id={id}
        type="checkbox"
        role="switch"
        checked={on}
        disabled={disabled}
        onChange={(e) => set(e.target.checked)}
      />
    </div>
  );
}
