import type { Recipe } from "@/lib/schemas";

export type RecipeView =
  | { kind: "metric"; metricId: string; dimensions: string[]; filters: [string, string][]; limit: number | null }
  | { kind: "sql"; source: string; sql: string }
  | { kind: "endpoint"; source: string; endpoint: string; params: [string, string][] }
  | { kind: "combine"; sql: string; inputs: { table: string; view: RecipeView }[] };

const text = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));
const pairs = (o: unknown) => Object.entries((o ?? {}) as Record<string, unknown>).map(([k, v]) => [k, text(v)] as [string, string]);

export function describeRecipe(r: Recipe): RecipeView {
  if (r.tool === "run_metric") {
    return { kind: "metric", metricId: r.args.metric_id, dimensions: r.args.dimensions, filters: pairs(r.args.filters),
      limit: r.args.limit };
  }
  if (r.tool === "query_source") {
    const req = r.args.request;
    if (typeof req.sql === "string") return { kind: "sql", source: r.args.source, sql: req.sql };
    return { kind: "endpoint", source: r.args.source, endpoint: text(req.endpoint_id ?? ""), params: pairs(req.params) };
  }
  return { kind: "combine", sql: r.args.sql,
    inputs: Object.entries(r.args.inputs).map(([table, child]) => ({ table, view: describeRecipe(child) })) };
}
