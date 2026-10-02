import { parseSse } from "@/lib/sse";
import {
  type ChatEvent, DashboardList, DashboardRun, type DashboardSummary, DevToken, KpisResponse, type KpiTile,
  type Recipe, ResultPage, type Widget,
} from "@/lib/schemas";

export const AGENT_URL = (process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class Unauthorized extends Error {
  constructor() { super("unauthorized"); }
}
export class ApiError extends Error {
  constructor(public status: number) { super(`request failed (${status})`); }
}

type Opts = { token?: string; method?: string; body?: unknown; signal?: AbortSignal };

async function request(path: string, { token, method = "GET", body, signal }: Opts = {}): Promise<Response> {
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${AGENT_URL}${path}`, { method, headers, signal,
      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0);
  }
  if (res.status === 401) throw new Unauthorized();
  if (!res.ok) throw new ApiError(res.status);
  return res;
}

const json = async (res: Response) => res.json() as Promise<unknown>;

export const api = {
  async devToken(personaId: string): Promise<string> {
    return DevToken.parse(await json(await request("/dev/token", { method: "POST", body: { persona_id: personaId } }))).token;
  },
  async kpis(token: string): Promise<KpiTile[]> {
    return KpisResponse.parse(await json(await request("/kpis", { token }))).tiles;
  },
  async results(token: string, handle: string, offset: number, limit: number): Promise<ResultPage> {
    const q = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return ResultPage.parse(await json(await request(`/results/${encodeURIComponent(handle)}?${q}`, { token })));
  },
  async confirm(token: string, recordId: string): Promise<void> {
    await request(`/answers/${encodeURIComponent(recordId)}/confirm`, { token, method: "POST" });
  },
  async *chat(token: string, question: string, signal: AbortSignal): AsyncGenerator<ChatEvent> {
    const res = await request("/chat", { token, method: "POST", body: { question }, signal });
    if (!res.body) throw new ApiError(0);
    yield* parseSse(res.body);
  },
  dashboards: {
    async list(token: string): Promise<DashboardSummary[]> {
      return DashboardList.parse(await json(await request("/dashboards", { token }))).dashboards;
    },
    async save(token: string, title: string, items: { widget: Widget; recipe: Recipe }[]): Promise<string> {
      const out = await json(await request("/dashboards", { token, method: "POST", body: { title, items } }));
      return (out as { id: string }).id;
    },
    async remove(token: string, id: string): Promise<void> {
      await request(`/dashboards/${encodeURIComponent(id)}`, { token, method: "DELETE" });
    },
    async run(token: string, id: string): Promise<DashboardRun> {
      return DashboardRun.parse(await json(await request(`/dashboards/${encodeURIComponent(id)}/run`,
        { token, method: "POST" })));
    },
  },
};
