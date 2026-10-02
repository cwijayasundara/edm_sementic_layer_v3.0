/* eslint-disable @typescript-eslint/no-explicit-any */
import type { EChartsOption } from "echarts";
import { tint } from "@/lib/charts";
import { LINEAGE_KINDS, type Lineage, type LineageKind, type LineageNode } from "@/lib/schemas";
import { sourceById } from "@/lib/sources";

export const KIND_LABEL: Record<LineageKind, string> = {
  Result: "Combined result", Metric: "Metric", Source: "Source", Dimension: "Dimension", Table: "Table",
  Endpoint: "Endpoint", BusinessTerm: "Business term", Column: "Column", Field: "Field", Question: "Past question",
};
export const NODE_SIZE: Record<LineageKind, number> = {
  Metric: 56, Result: 48, Source: 44, Table: 36, Endpoint: 36, Dimension: 28, BusinessTerm: 28, Question: 28,
  Column: 20, Field: 20,
};
const NAVY = "#14213d", TERM = "#2f8f83", QUESTION = "#d08a1c", NEUTRAL = "#5a6478";

export function nodeColor(n: LineageNode): string {
  if (n.kind === "Metric" || n.kind === "Result") return NAVY;
  if (n.kind === "BusinessTerm") return TERM;
  if (n.kind === "Question") return QUESTION;
  const base = sourceById(n.source ?? (n.kind === "Source" ? n.label : null))?.hex ?? NEUTRAL;
  return n.kind === "Source" || n.kind === "Table" || n.kind === "Endpoint" ? base : tint(base, 0.45);
}

export function shortLabel(s: string, max = 18): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

/** Lineage -> one ECharts force graph. Node `name` is the node id (ECharts links resolve by name). */
export function toGraphOption(g: Lineage): EChartsOption {
  const kinds = LINEAGE_KINDS.filter((k) => g.nodes.some((n) => n.kind === k));
  return {
    tooltip: { formatter: (p: any) => (p.dataType === "edge" ? p.data.type : `${KIND_LABEL[p.data.kind as LineageKind]}: ${p.data.fullLabel}`) },
    legend: { data: kinds.map((k) => KIND_LABEL[k]), bottom: 0, type: "scroll", icon: "circle" },
    series: [{
      type: "graph", layout: "force", roam: true, draggable: true,
      force: { repulsion: 220, edgeLength: [50, 120], gravity: 0.08 },
      categories: kinds.map((k) => ({ name: KIND_LABEL[k] })),
      label: { show: true, position: "bottom", fontSize: 10, color: "#3a4357", formatter: (p: any) => shortLabel(p.data.fullLabel) },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 6,
      lineStyle: { color: "#c3cad6", width: 1, curveness: 0.08 },
      emphasis: { focus: "adjacency", lineStyle: { width: 2 } },
      data: g.nodes.map((n) => ({ name: n.id, fullLabel: n.label, kind: n.kind, category: kinds.indexOf(n.kind),
        symbolSize: NODE_SIZE[n.kind], itemStyle: { color: nodeColor(n) } })),
      links: g.edges.map((e) => ({ source: e.from, target: e.to, type: e.type })),
    }],
  } as EChartsOption;
}
