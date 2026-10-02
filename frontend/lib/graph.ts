/* eslint-disable @typescript-eslint/no-explicit-any */
import type { EChartsOption } from "echarts";
import { type Lineage, type LineageKind, type LineageNode } from "@/lib/schemas";
import { SOURCES, sourceById } from "@/lib/sources";

export const KIND_LABEL: Record<LineageKind, string> = {
  Result: "Combined result", Metric: "Metric", Source: "Source", Dimension: "Dimension", Table: "Table",
  Endpoint: "Endpoint", BusinessTerm: "Business term", Column: "Column", Field: "Field", Question: "Past question",
};
export const NODE_SIZE: Record<LineageKind, number> = {
  Metric: 56, Result: 48, Source: 44, Table: 36, Endpoint: 36, Dimension: 28, BusinessTerm: 28, Question: 28,
  Column: 20, Field: 20,
};
/** Colour says which system a node came from, so the kind is told by shape (an ECharts symbol). */
export const NODE_SHAPE: Record<LineageKind, string> = {
  Metric: "circle", Result: "circle", Source: "roundRect", Table: "rect", Endpoint: "rect", Dimension: "triangle",
  Column: "diamond", Field: "diamond", BusinessTerm: "pin", Question: "pin",
};
/** The shape key shown beside the graph, in the order a reader meets the kinds. */
export const SHAPE_KEY: { shape: string; kinds: LineageKind[] }[] = [
  { shape: "circle", kinds: ["Metric", "Result"] }, { shape: "roundRect", kinds: ["Source"] },
  { shape: "rect", kinds: ["Table", "Endpoint"] }, { shape: "triangle", kinds: ["Dimension"] },
  { shape: "diamond", kinds: ["Column", "Field"] }, { shape: "pin", kinds: ["BusinessTerm", "Question"] },
];
const NAVY = "#14213d", NEUTRAL = "#5a6478", HIGHLIGHT = "#e0a526";

/** Where a node came from: one of the source systems, or Prism's own knowledge (combined results, glossary terms,
 *  past questions), which is not read from any source. */
export type Origin = { id: string; name: string; hex: string };
export const PRISM_ORIGIN: Origin = { id: "prism", name: "Prism knowledge", hex: NAVY };

export function nodeOrigin(n: LineageNode): Origin {
  const id = n.source ?? (n.kind === "Source" ? n.label : null);
  if (!id) return PRISM_ORIGIN;
  const s = sourceById(id);
  return s ? { id: s.id, name: s.name, hex: s.hex } : { id, name: id, hex: NEUTRAL };
}

/** How the answer reached a node of this kind in its source, when the kind alone says so. */
export function accessOf(kind: LineageKind): string | null {
  if (kind === "Table" || kind === "Column") return "SQL database";
  if (kind === "Endpoint" || kind === "Field") return "REST API";
  return null;
}

export type SourceUse = Origin & { access: string[]; count: number };

/** The source systems behind a lineage (never Prism), in SOURCES order, each with how it was reached and how many
 *  nodes came from it. Built from the gated nodes only, so a source the caller cannot see is never named. */
export function sourcesUsed(g: Lineage): SourceUse[] {
  const uses = new Map<string, SourceUse>();
  for (const n of g.nodes) {
    const o = nodeOrigin(n);
    if (o === PRISM_ORIGIN) continue;
    const u = uses.get(o.id) ?? { ...o, access: [], count: 0 };
    u.count += 1;
    const a = accessOf(n.kind);
    if (a && !u.access.includes(a)) u.access.push(a);
    uses.set(o.id, u);
  }
  for (const u of uses.values()) {
    if (u.access.length === 0) {
      const fallback = SOURCES.find((s) => s.id === u.id)?.access;
      if (fallback) u.access.push(fallback);
    }
    u.access.sort();
  }
  const rank = (id: string) => { const i = SOURCES.findIndex((s) => s.id === id); return i < 0 ? SOURCES.length : i; };
  return [...uses.values()].sort((a, b) => rank(a.id) - rank(b.id) || a.id.localeCompare(b.id));
}

/** "From MarketMaster · SQL database" for a node (its own access when its kind tells it, else its source's). */
export function originText(n: LineageNode, uses: SourceUse[]): string {
  const o = nodeOrigin(n);
  if (o === PRISM_ORIGIN) return `From ${o.name}`;
  const access = accessOf(n.kind) ?? uses.find((u) => u.id === o.id)?.access.join(" + ");
  return access ? `From ${o.name} · ${access}` : `From ${o.name}`;
}

export function nodeColor(n: LineageNode): string {
  return nodeOrigin(n).hex;
}

export function shortLabel(s: string, max = 18): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

const escapeHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

/** Lineage -> one ECharts force graph, one legend category per origin. Node `name` is the node id (ECharts links
 *  resolve by name). */
export function toGraphOption(g: Lineage, opts: { highlight?: Set<string> } = {}): EChartsOption {
  const origins = [...sourcesUsed(g).map(({ id, name, hex }) => ({ id, name, hex })),
    ...(g.nodes.some((n) => nodeOrigin(n) === PRISM_ORIGIN) ? [PRISM_ORIGIN] : [])];
  const category = (n: LineageNode) => origins.findIndex((o) => o.id === nodeOrigin(n).id);
  return {
    tooltip: { formatter: (p: any) => (p.dataType === "edge" ? escapeHtml(p.data.type)
      : `${KIND_LABEL[p.data.kind as LineageKind]}: ${escapeHtml(p.data.fullLabel)}<br/>From ${escapeHtml(p.data.origin)}`) },
    legend: { data: origins.map((o) => o.name), bottom: 0, type: "scroll", icon: "circle" },
    series: [{
      type: "graph", layout: "force", roam: true, draggable: true,
      force: { repulsion: 220, edgeLength: [50, 120], gravity: 0.08 },
      categories: origins.map((o) => ({ name: o.name, itemStyle: { color: o.hex } })),
      label: { show: true, position: "bottom", fontSize: 10, color: "#3a4357", formatter: (p: any) => shortLabel(p.data.fullLabel) },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 6,
      lineStyle: { color: "#c3cad6", width: 1, curveness: 0.08 },
      emphasis: { focus: "adjacency", lineStyle: { width: 2 } },
      data: g.nodes.map((n) => ({ name: n.id, fullLabel: n.label, kind: n.kind, origin: nodeOrigin(n).name,
        category: category(n), symbol: NODE_SHAPE[n.kind], symbolSize: NODE_SIZE[n.kind], itemStyle: { color: nodeColor(n),
          ...(opts.highlight?.has(n.id) ? { borderColor: HIGHLIGHT, borderWidth: 3 } : {}) } })),
      links: g.edges.map((e) => ({ source: e.from, target: e.to, type: e.type })),
    }],
  } as EChartsOption;
}
