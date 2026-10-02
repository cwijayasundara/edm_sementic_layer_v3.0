import { z } from "zod";

const KEY = "prism.token";
const Claims = z.object({
  sub: z.string(), name: z.string(), roles: z.array(z.string()), scopes: z.array(z.string()),
  rows: z.record(z.array(z.string())), metrics_only: z.boolean(), exp: z.number(),
});
export type Claims = z.infer<typeof Claims>;

const SOURCE_NAMES: Record<string, string> = {
  refmaster: "RefMaster", marketmaster: "MarketMaster", cashrecon: "CashRecon", assetrecon: "AssetRecon",
  feedhub: "FeedHub",
};

/** The JWT payload, for display only: the agent and gateway verify the token and enforce access. */
export function decodeClaims(token: string): Claims | null {
  const part = token.split(".")[1];
  if (!part) return null;
  try {
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0));
    const parsed = Claims.safeParse(JSON.parse(new TextDecoder().decode(bytes)));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

export function isExpired(c: Claims, nowMs: number = Date.now()): boolean {
  return c.exp * 1000 - 60_000 <= nowMs;
}

function storage(): Storage | null {
  try { return typeof window === "undefined" ? null : window.sessionStorage; } catch { return null; }
}

export function loadSession(): { token: string; claims: Claims } | null {
  const token = storage()?.getItem(KEY);
  const claims = token ? decodeClaims(token) : null;
  return token && claims && !isExpired(claims) ? { token, claims } : null;
}

export function saveToken(token: string): void { storage()?.setItem(KEY, token); }
export function clearSession(): void { storage()?.removeItem(KEY); }

export function accessChips(c: Claims): string[] {
  const chips: string[] = [];
  for (const scope of c.scopes) {
    if (scope === "pii:read") { chips.push("PII"); continue; }
    const [db, table] = scope.split(".");
    const name = SOURCE_NAMES[db] ?? db;
    chips.push(table ? `${name} ${table.replace(/_/g, " ")} ✓` : `${name} ✓`);
  }
  for (const [dim, values] of Object.entries(c.rows)) {
    if (values.includes("*")) continue;
    chips.push(`${dim.replace(/_/g, " ")} ${values.join(", ")}`);
  }
  if (c.metrics_only) chips.push("metrics only");
  chips.push("RBAC + RLS");
  return chips;
}
