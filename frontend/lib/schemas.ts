import { z } from "zod";

export const WIDGET_TYPES = ["kpi", "bar", "stacked_bar", "line", "heatmap", "table", "pie", "scatter"] as const;
export type WidgetType = (typeof WIDGET_TYPES)[number];

const col = z.string().nullable().optional();
export const Encoding = z.object({ x: col, y: col, series: col, value: col, unit: col });
export type Encoding = z.infer<typeof Encoding>;

export const Widget = z.object({
  id: z.string(),
  type: z.enum(WIDGET_TYPES),
  title: z.string().min(1).max(120),
  handle: z.string(),
  encoding: Encoding,
});
export type Widget = z.infer<typeof Widget>;

export type Recipe =
  | { tool: "run_metric"; args: { metric_id: string; dimensions: string[]; filters: Record<string, unknown>; limit: number | null } }
  | { tool: "query_source"; args: { source: string; request: Record<string, unknown> } }
  | { tool: "combine"; args: { sql: string; inputs: Record<string, Recipe> } };

export const Recipe: z.ZodType<Recipe> = z.lazy(() =>
  z.union([
    z.object({ tool: z.literal("run_metric"), args: z.object({ metric_id: z.string(),
      dimensions: z.array(z.string()).default([]), filters: z.record(z.unknown()).default({}),
      limit: z.number().nullable().default(null) }) }),
    z.object({ tool: z.literal("query_source"), args: z.object({ source: z.string(), request: z.record(z.unknown()) }) }),
    z.object({ tool: z.literal("combine"), args: z.object({ sql: z.string(), inputs: z.record(Recipe) }) }),
  ]),
) as z.ZodType<Recipe>;

export const HandleInfo = z.object({
  columns: z.array(z.string()),
  row_count: z.number(),
  source: z.string().nullable(),
  metric_id: z.string().nullable(),
  recipe: Recipe.nullable().optional(),
});
export type HandleInfo = z.infer<typeof HandleInfo>;

export const PlanEvent = z.object({ type: z.literal("plan"), tool: z.string(), label: z.string() });
export const WidgetEvent = z.object({ type: z.literal("widget"), widget: Widget, handle_info: HandleInfo });
export const SummaryEvent = z.object({ type: z.literal("summary"), text: z.string() });
export const TelemetryEvent = z.object({
  type: z.literal("telemetry"), run_id: z.string(), path: z.string().nullable(), models: z.array(z.string()),
  input_tokens: z.number(), output_tokens: z.number(), cache_read_input_tokens: z.number(), llm_turns: z.number(),
  tool_calls: z.number(), tool_latency_ms: z.number(), cost_usd: z.number(),
});
export const ErrorEvent = z.object({ type: z.literal("error"), code: z.string(), message: z.string() });
export const AnswerEvent = z.object({ type: z.literal("answer"), record_id: z.string().uuid(), confirmable: z.boolean() });
export const ChatEvent = z.discriminatedUnion("type", [PlanEvent, WidgetEvent, SummaryEvent, AnswerEvent, TelemetryEvent, ErrorEvent]);
export type PlanEvent = z.infer<typeof PlanEvent>;
export type WidgetEvent = z.infer<typeof WidgetEvent>;
export type SummaryEvent = z.infer<typeof SummaryEvent>;
export type AnswerEvent = z.infer<typeof AnswerEvent>;
export type TelemetryEvent = z.infer<typeof TelemetryEvent>;
export type ErrorEvent = z.infer<typeof ErrorEvent>;
export type ChatEvent = z.infer<typeof ChatEvent>;
export const CHAT_EVENT_TYPES = new Set(["plan", "widget", "summary", "answer", "telemetry", "error"]);

export const KpiTile = z.object({
  label: z.string(), metric_id: z.string(), unit: z.string().default(""),
  status: z.enum(["ok", "unavailable"]), value: z.number().nullable().optional(), source: z.string().nullable().optional(),
});
export type KpiTile = z.infer<typeof KpiTile>;
export const KpisResponse = z.object({ tiles: z.array(KpiTile) });

export const ResultPage = z.object({
  handle: z.string(), columns: z.array(z.string()), offset: z.number(), row_count: z.number(),
  rows: z.array(z.array(z.unknown())),
});
export type ResultPage = z.infer<typeof ResultPage>;

export const DashboardSummary = z.object({ id: z.string(), title: z.string(), created_at: z.string(),
  widget_count: z.number() });
export type DashboardSummary = z.infer<typeof DashboardSummary>;
export const DashboardList = z.object({ dashboards: z.array(DashboardSummary) });
export const RunStatus = z.enum(["ok", "not_permitted", "unavailable", "invalid"]);
export type RunStatus = z.infer<typeof RunStatus>;
export const RunWidget = z.object({ widget: Widget, status: RunStatus, handle_info: HandleInfo.optional() });
export type RunWidget = z.infer<typeof RunWidget>;
export const DashboardRun = z.object({ id: z.string(), title: z.string(), widgets: z.array(RunWidget) });
export type DashboardRun = z.infer<typeof DashboardRun>;
export const DevToken = z.object({ token: z.string() });
export type DevToken = z.infer<typeof DevToken>;

export const LINEAGE_KINDS = ["Result", "Metric", "Source", "Dimension", "Table", "Endpoint", "BusinessTerm", "Column",
  "Field", "Question"] as const;
export type LineageKind = (typeof LINEAGE_KINDS)[number];
export const LineageNode = z.object({ id: z.string(), kind: z.enum(LINEAGE_KINDS), label: z.string(),
  source: z.string().optional(), detail: z.string().optional() });
export type LineageNode = z.infer<typeof LineageNode>;
export const LineageEdge = z.object({ from: z.string(), to: z.string(), type: z.string() });
export const Lineage = z.object({ nodes: z.array(LineageNode), edges: z.array(LineageEdge), truncated: z.boolean(),
  governed: z.boolean() });
export type Lineage = z.infer<typeof Lineage>;
