const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
export const TOKEN = `${b64({ alg: "HS256" })}.${b64({ sub: "cash_ops_emea", name: "Cash Ops Analyst - EMEA",
  roles: ["cash_ops_emea"], scopes: ["cashrecon", "feedhub"], rows: { region: ["EMEA"] }, metrics_only: false,
  exp: Math.floor(Date.now() / 1000) + 3600 })}.sig`;

export const KPIS = { tiles: [
  { label: "Open breaks", metric_id: "open_breaks", unit: "", status: "ok", value: 12 },
  { label: "Auto-match rate", metric_id: "auto_match_rate", unit: "%", status: "ok", value: 91.2 },
] };

export const RECORD_ID = "11111111-1111-4111-8111-111111111111";
export const RECIPE = { tool: "run_metric", args: { metric_id: "open_breaks", dimensions: ["region"], filters: {}, limit: null } };
export const WIDGET = { id: "w1", type: "bar", title: "Open breaks by region", handle: "r_aaaaaaaaaaaa",
  encoding: { x: "region", y: "value", series: null, value: null, unit: null } };

const frame = (o: { type: string; [k: string]: unknown }) => `event: ${o.type}\ndata: ${JSON.stringify(o)}\n\n`;
export const chatBody = (handle: string) => [
  frame({ type: "plan", tool: "run_metric", label: "metric open_breaks" }),
  frame({ type: "widget", widget: { ...WIDGET, handle }, handle_info: { columns: ["region", "value"], row_count: 60,
    source: "cashrecon", metric_id: "open_breaks", recipe: RECIPE } }),
  frame({ type: "summary", text: "**EMEA** has the most open breaks." }),
  frame({ type: "answer", record_id: RECORD_ID, confirmable: true }),
  frame({ type: "telemetry", run_id: "r1", path: "metric", models: ["m"], input_tokens: 1000, output_tokens: 100,
    cache_read_input_tokens: 800, llm_turns: 2, tool_calls: 2, tool_latency_ms: 20, cost_usd: 0.01 }),
].join("");
export const CHAT_BODY = chatBody("r_aaaaaaaaaaaa");

export function rows(offset: number, limit: number) {
  const all = Array.from({ length: 60 }, (_, i) => [i === 0 ? "EMEA" : `EMEA-${i}`, 60 - i]);
  return { handle: "r_aaaaaaaaaaaa", columns: ["region", "value"], offset, row_count: 60, rows: all.slice(offset, offset + limit) };
}
