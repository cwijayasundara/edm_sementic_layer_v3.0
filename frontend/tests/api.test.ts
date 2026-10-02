// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { AGENT_URL, ApiError, Unauthorized, api } from "@/lib/api";

function reply(status: number, body?: unknown, headers: Record<string, string> = {}) {
  return vi.fn().mockResolvedValue(new Response(body === undefined ? null : typeof body === "string" ? body
    : JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } }));
}

afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("sends the bearer token and parses KPI tiles", async () => {
    const f = reply(200, { tiles: [{ label: "Open breaks", metric_id: "open_breaks", unit: "", status: "ok", value: 3 }] });
    vi.stubGlobal("fetch", f);
    expect((await api.kpis("T"))[0].value).toBe(3);
    const [url, init] = f.mock.calls[0];
    expect(url).toBe(`${AGENT_URL}/kpis`);
    expect(init.headers.Authorization).toBe("Bearer T");
  });

  it("maps 401 to Unauthorized and other failures to ApiError with the status", async () => {
    vi.stubGlobal("fetch", reply(401, { detail: "unauthorized" }));
    await expect(api.kpis("T")).rejects.toBeInstanceOf(Unauthorized);
    vi.stubGlobal("fetch", reply(502, { detail: "data service unavailable" }));
    await expect(api.results("T", "r_aaaaaaaaaaaa", 0, 50)).rejects.toMatchObject({ status: 502 });
    await expect(api.results("T", "r_aaaaaaaaaaaa", 0, 50)).rejects.toBeInstanceOf(ApiError);
  });

  it("builds the results query and posts dashboards", async () => {
    const f = reply(200, { handle: "r_aaaaaaaaaaaa", columns: ["a"], offset: 50, row_count: 60, rows: [[1]] });
    vi.stubGlobal("fetch", f);
    await api.results("T", "r_aaaaaaaaaaaa", 50, 50);
    expect(f.mock.calls[0][0]).toBe(`${AGENT_URL}/results/r_aaaaaaaaaaaa?offset=50&limit=50`);
    const g = reply(201, { id: "d1" });
    vi.stubGlobal("fetch", g);
    const recipe = { tool: "run_metric" as const, args: { metric_id: "m", dimensions: [], filters: {}, limit: null } };
    const widget = { id: "w1", type: "table" as const, title: "t", handle: "r_aaaaaaaaaaaa", encoding: {} };
    expect(await api.dashboards.save("T", "Board", [{ widget, recipe }])).toBe("d1");
    expect(JSON.parse(g.mock.calls[0][1].body)).toEqual({ title: "Board", items: [{ widget, recipe }] });
  });

  it("streams chat events", async () => {
    vi.stubGlobal("fetch", reply(200, 'event: summary\ndata: {"type":"summary","text":"hi"}\n\n',
      { "content-type": "text/event-stream" }));
    const out = [];
    for await (const e of api.chat("T", "q", new AbortController().signal)) out.push(e);
    expect(out).toEqual([{ type: "summary", text: "hi" }]);
  });
});
