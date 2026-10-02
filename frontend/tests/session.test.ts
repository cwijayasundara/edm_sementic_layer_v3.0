import { beforeEach, describe, expect, it } from "vitest";
import { accessChips, clearSession, decodeClaims, isExpired, loadSession, saveToken } from "@/lib/session";

function jwt(payload: object) {
  const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64('{"alg":"HS256"}')}.${b64(JSON.stringify(payload))}.sig`;
}
const cash = { sub: "cash_ops_emea", name: "Cash Ops Analyst - EMEA", roles: ["cash_ops_emea"],
  scopes: ["cashrecon", "feedhub"], rows: { region: ["EMEA"], source_type: ["bank"] }, metrics_only: false,
  exp: Math.floor(Date.now() / 1000) + 3600 };

beforeEach(() => sessionStorage.clear());

describe("session", () => {
  it("decodes claims for display and rejects garbage", () => {
    expect(decodeClaims(jwt(cash))?.name).toBe("Cash Ops Analyst - EMEA");
    expect(decodeClaims("nope")).toBeNull();
    expect(decodeClaims(jwt({ sub: 1 }))).toBeNull();
  });

  it("treats a token as expired a minute early", () => {
    const c = decodeClaims(jwt(cash))!;
    expect(isExpired(c, c.exp * 1000 - 61_000)).toBe(false);
    expect(isExpired(c, c.exp * 1000 - 59_000)).toBe(true);
  });

  it("stores, loads and clears the session; an expired token loads as none", () => {
    saveToken(jwt(cash));
    expect(loadSession()?.claims.sub).toBe("cash_ops_emea");
    saveToken(jwt({ ...cash, exp: 1 }));
    expect(loadSession()).toBeNull();
    saveToken(jwt(cash));
    clearSession();
    expect(loadSession()).toBeNull();
  });

  it("derives access chips from scopes and row grants", () => {
    expect(accessChips(cash)).toEqual(["CashRecon ✓", "FeedHub ✓", "region EMEA", "source type bank", "RBAC + RLS"]);
    const bi = { ...cash, scopes: ["refmaster", "refmaster.securities", "pii:read"], rows: { region: ["*"] },
      metrics_only: true };
    expect(accessChips(bi)).toEqual(["RefMaster ✓", "RefMaster securities ✓", "PII", "metrics only", "RBAC + RLS"]);
  });
});
