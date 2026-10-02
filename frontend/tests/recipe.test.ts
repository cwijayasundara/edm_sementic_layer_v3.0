import { describe, expect, it } from "vitest";
import { describeRecipe } from "@/lib/recipe";

describe("describeRecipe", () => {
  it("describes metrics, SQL, REST endpoints and combine trees", () => {
    expect(describeRecipe({ tool: "run_metric", args: { metric_id: "open_breaks", dimensions: ["region"],
      filters: { ccy: "USD", age: { gte: 5 } }, limit: null } })).toEqual({ kind: "metric", metricId: "open_breaks",
      dimensions: ["region"], filters: [["ccy", "USD"], ["age", '{"gte":5}']], limit: null });
    expect(describeRecipe({ tool: "query_source", args: { source: "cashrecon", request: { sql: "SELECT 1" } } }))
      .toEqual({ kind: "sql", source: "cashrecon", sql: "SELECT 1" });
    expect(describeRecipe({ tool: "query_source", args: { source: "refmaster",
      request: { endpoint_id: "securities", params: { isin: "X" } } } })).toEqual({ kind: "endpoint",
      source: "refmaster", endpoint: "securities", params: [["isin", "X"]] });
    const tree = describeRecipe({ tool: "combine", args: { sql: "SELECT * FROM a", inputs: {
      a: { tool: "query_source", args: { source: "s", request: { sql: "SELECT 2" } } } } } });
    expect(tree).toEqual({ kind: "combine", sql: "SELECT * FROM a",
      inputs: [{ table: "a", view: { kind: "sql", source: "s", sql: "SELECT 2" } }] });
  });
});
