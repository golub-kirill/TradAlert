// Small display helpers shared across views.

export function fnum(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return "—";
  return (v * 100).toFixed(digits) + "%";
}

// R multiple with explicit sign, e.g. "+1.23R" / "-0.48R".
export function rstr(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return (v > 0 ? "+" : "") + v.toFixed(digits) + "R";
}

// css class for a signed value (green positive / red negative).
export function signClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "";
  return v < 0 ? "neg" : "pos";
}

export function tickerOk(t: string): boolean {
  return /^[A-Za-z0-9.\-]{1,12}$/.test(t.trim());
}

export const today = (): string => new Date().toISOString().slice(0, 10);
