const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact",
  maximumFractionDigits: 1 });
const num = (v: number) => new Intl.NumberFormat("en-US", { maximumFractionDigits: Number.isInteger(v) ? 0 : 2 }).format(v);

export function formatValue(v: unknown, unit?: string | null): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return v == null || typeof v === "number" ? "—" : String(v);
  if (unit === "%") return `${v.toFixed(1)}%`;
  if (unit === "USD") return usd.format(v);
  return unit ? `${num(v)} ${unit}` : num(v);
}

export function formatCell(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "number") return num(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
