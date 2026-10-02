import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

import { SessionProvider, useSession } from "@/components/SessionProvider";
import { Unauthorized } from "@/lib/api";
import { saveToken } from "@/lib/session";

function jwt(payload: object) {
  const b64 = (s: string) => btoa(s).replace(/=+$/, "");
  return `${b64("{}")}.${b64(JSON.stringify(payload))}.s`;
}

function Probe({ fail }: { fail: boolean }) {
  const { call } = useSession();
  void call(async () => { if (fail) throw new Unauthorized(); return 1; }).catch(() => undefined);
  return null;
}

beforeEach(() => { sessionStorage.clear(); replace.mockReset(); });

describe("SessionProvider", () => {
  it("redirects to /login without a session", async () => {
    render(<SessionProvider><Probe fail={false} /></SessionProvider>);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("clears the session and redirects with expired=1 on a 401", async () => {
    saveToken(jwt({ sub: "head_data", name: "Head", roles: [], scopes: [], rows: {}, metrics_only: false,
      exp: Math.floor(Date.now() / 1000) + 3600 }));
    render(<SessionProvider><Probe fail /></SessionProvider>);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login?expired=1"));
    expect(sessionStorage.length).toBe(0);
  });
});
