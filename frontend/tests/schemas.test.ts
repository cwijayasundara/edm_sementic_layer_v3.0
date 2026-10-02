import { describe, expect, it } from "vitest";
import { ChatEvent, DashboardRun, KpisResponse, Recipe, ResultPage } from "@/lib/schemas";

describe("schemas mirror the agent contracts", () => {
  it("accepts the carry-forward widget event, with and without a recipe", () => {
    const ev = {
      type: "widget",
      widget: { id: "w1", type: "bar", title: "Open breaks by region", handle: "r_aaaaaaaaaaaa",
        encoding: { x: "region", y: "value", series: null, value: null, unit: null } },
      handle_info: { columns: ["region", "value"], row_count: 4, source: "cashrecon", metric_id: "open_breaks" },
    };
    expect(ChatEvent.parse(ev).type).toBe("widget");
    const withRecipe = { ...ev, handle_info: { ...ev.handle_info, recipe: { tool: "run_metric",
      args: { metric_id: "open_breaks", dimensions: ["region"], filters: {}, limit: null } } } };
    expect(ChatEvent.parse(withRecipe)).toMatchObject({ handle_info: { recipe: { tool: "run_metric" } } });
  });

  it("accepts telemetry and error events and rejects an unknown widget type", () => {
    expect(ChatEvent.parse({ type: "telemetry", run_id: "9f0c", path: "metric", models: ["m"], input_tokens: 1,
      output_tokens: 2, cache_read_input_tokens: 0, llm_turns: 1, tool_calls: 1, tool_latency_ms: 3.5,
      cost_usd: 0.01 }).type).toBe("telemetry");
    expect(ChatEvent.parse({ type: "error", code: "run_limit", message: "m" }).type).toBe("error");
    expect(ChatEvent.safeParse({ type: "widget", widget: { id: "w", type: "radar", title: "t", handle: "h",
      encoding: {} }, handle_info: { columns: [], row_count: 0, source: null, metric_id: null } }).success).toBe(false);
  });

  it("parses nested combine recipes, KPI tiles, result pages and dashboard runs", () => {
    const metric = { tool: "run_metric", args: { metric_id: "m", dimensions: [], filters: {}, limit: null } };
    expect(Recipe.parse({ tool: "combine", args: { sql: "SELECT 1", inputs: { a: metric } } }).tool).toBe("combine");
    expect(KpisResponse.parse({ tiles: [{ label: "Open breaks", metric_id: "open_breaks", unit: "", status: "ok",
      value: 12 }, { label: "Late", metric_id: "late_feeds", unit: "", status: "unavailable" }] }).tiles).toHaveLength(2);
    expect(ResultPage.parse({ handle: "r_aaaaaaaaaaaa", columns: ["a"], offset: 0, row_count: 1, rows: [[1]] })
      .rows).toEqual([[1]]);
    expect(DashboardRun.parse({ id: "x", title: "t", widgets: [{ widget: { id: "w1", type: "table", title: "t",
      handle: "", encoding: {} }, status: "not_permitted" }] }).widgets[0].status).toBe("not_permitted");
  });
});
