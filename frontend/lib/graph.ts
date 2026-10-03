/* eslint-disable @typescript-eslint/no-explicit-any */
import type { EChartsOption } from "echarts";
import { type Lineage, type LineageKind, type LineageNode } from "@/lib/schemas";
import { SOURCES, sourceById } from "@/lib/sources";

export const KIND_LABEL: Record<LineageKind, string> = {
  Result: "Combined result", Metric: "Metric", Source: "Source", Dimension: "Dimension", Table: "Table",
  Endpoint: "Endpoint", BusinessTerm: "Business term", Column: "Column", Field: "Field", Question: "Past question",
};
/** Full-size node diameters in pixels (see nodeScale for big graphs). */
export const NODE_SIZE: Record<LineageKind, number> = {
  Metric: 96, Result: 90, Source: 90, Table: 84, Endpoint: 84, Dimension: 78, BusinessTerm: 78, Question: 78,
  Column: 72, Field: 72,
};

/** Each kind has its own fill, an icon (24x24 stroke paths) and a short name drawn inside the node; the ring around
 *  the node is the source system's colour. `ink` is the icon and text colour that reads on the fill. */
export type KindStyle = { fill: string; ink: string; short: string; icon: string };
const WHITE = "#ffffff", INK = "#14213d", HIGHLIGHT = "#e0a526";
export const KIND_STYLE: Record<LineageKind, KindStyle> = {
  Metric: { fill: "#3b5bdb", ink: WHITE, short: "Metric",
    icon: '<path d="M3 3v18h18"/><path d="M7 16v-4"/><path d="M12 16V8"/><path d="M17 16v-7"/>' },
  Result: { fill: "#14213d", ink: WHITE, short: "Result", icon: '<path d="M18 7V4H6l6 8-6 8h12v-3"/>' },
  Source: { fill: "#8e5bd0", ink: WHITE, short: "Source",
    icon: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>' },
  Table: { fill: "#5f84ad", ink: WHITE, short: "Table",
    icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M12 3v18"/>' },
  Endpoint: { fill: "#2f8f9d", ink: WHITE, short: "Endpoint",
    icon: '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>' },
  Column: { fill: "#7cc0ea", ink: INK, short: "Column",
    icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="M15 3v18"/>' },
  Field: { fill: "#8fd3c7", ink: INK, short: "Field",
    icon: '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5a2 2 0 0 0 2 2h1"/>'
      + '<path d="M16 21h1a2 2 0 0 0 2-2v-5a2 2 0 0 1 2-2 2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>' },
  Dimension: { fill: "#f2bb4b", ink: INK, short: "Dimension",
    icon: '<path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>' },
  BusinessTerm: { fill: "#3f7d5c", ink: WHITE, short: "Term",
    icon: '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>' },
  Question: { fill: "#e07a5f", ink: WHITE, short: "Question",
    icon: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>' },
};

/** The node as one SVG image: a white gap and the source-coloured ring around the kind's fill, its icon and short
 *  name inside, and a gold halo when the answer's reasoning touched it. */
export function nodeSvg(kind: LineageKind, ringHex: string, highlighted = false): string {
  const k = KIND_STYLE[kind];
  const halo = highlighted ? `<circle cx="50" cy="50" r="47.5" fill="none" stroke="${HIGHLIGHT}" stroke-width="4.5"/>` : "";
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' + halo
    + `<circle cx="50" cy="50" r="44" fill="${ringHex}"/><circle cx="50" cy="50" r="40.5" fill="#ffffff"/>`
    + `<circle cx="50" cy="50" r="38" fill="${k.fill}"/>`
    + `<g transform="translate(37.4 23) scale(1.05)" fill="none" stroke="${k.ink}" stroke-width="2.2" stroke-linecap="round"`
    + ` stroke-linejoin="round">${k.icon}</g>`
    + `<text x="50" y="68" text-anchor="middle" font-family="system-ui,-apple-system,Segoe UI,sans-serif"`
    + ` font-size="${k.short.length > 7 ? 10.5 : 12}" font-weight="600" fill="${k.ink}">${k.short}</text></svg>`;
}

export const nodeSymbol = (kind: LineageKind, ringHex: string, highlighted = false) =>
  `image://data:image/svg+xml;charset=utf-8,${encodeURIComponent(nodeSvg(kind, ringHex, highlighted))}`;

/** "READS_FROM" -> "reads from". */
export const edgeText = (type: string) => type.toLowerCase().replace(/_/g, " ");
/** Above this many edges, edge names show only on hover so the graph stays readable. */
export const EDGE_LABEL_MAX = 40;
/** Above this many nodes, node names that overlap are hidden (zoom in or hover to read them). */
export const DENSE_NODES = 30;

const NAVY = "#14213d", NEUTRAL = "#5a6478";

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

export function shortLabel(s: string, max = 40): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

const escapeHtml = (s: string) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

/** Left to right, the way an answer is traced: what asked for it, the metrics, where they come from, the tables or
 *  endpoints behind them, then their columns or fields. */
export const KIND_RANK: Record<LineageKind, number> = {
  Question: 0, BusinessTerm: 0, Result: 0, Metric: 1, Dimension: 2, Source: 2, Table: 3, Endpoint: 3, Column: 4, Field: 4,
};
/** A rank taller than this (or than sqrt(2n) for a big rank) wraps into sub-columns, so a pile of columns does not
 *  stretch the graph into a thin strip. */
export const RANK_ROWS = 8;
const COL_GAP = 280, SUB_GAP = 200, ROW_GAP = 170;

/** Fixed positions: one band of sub-columns per occupied rank, each band ordered by where its neighbours in earlier
 *  bands sit (one barycentre pass) so lines cross less. Deterministic for a given lineage. */
export function layeredPositions(g: Lineage): Map<string, { x: number; y: number }> {
  const ranks = [...new Set(g.nodes.map((n) => KIND_RANK[n.kind]))].sort((a, b) => a - b);
  const pos = new Map<string, { x: number; y: number }>();
  const neighbours = new Map<string, string[]>();
  for (const e of g.edges) {
    neighbours.set(e.from, [...(neighbours.get(e.from) ?? []), e.to]);
    neighbours.set(e.to, [...(neighbours.get(e.to) ?? []), e.from]);
  }
  let x = 0;
  for (const r of ranks) {
    const order = new Map(g.nodes.map((n, i) => [n.id, i]));
    const key = (id: string) => {
      const ys = (neighbours.get(id) ?? []).map((m) => pos.get(m)?.y).filter((y): y is number => y !== undefined);
      return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Number.POSITIVE_INFINITY;
    };
    const band = g.nodes.filter((n) => KIND_RANK[n.kind] === r)
      .map((n) => ({ id: n.id, k: key(n.id) }))
      .sort((a, b) => (a.k === b.k ? order.get(a.id)! - order.get(b.id)! : a.k < b.k ? -1 : 1));
    const subs = Math.ceil(band.length / Math.max(RANK_ROWS, Math.ceil(Math.sqrt(2 * band.length))));
    const rows = Math.ceil(band.length / subs);
    band.forEach(({ id }, i) => {
      const sub = Math.floor(i / rows), row = i % rows;
      const inSub = Math.min(rows, band.length - sub * rows);
      // stagger sub-columns by half a row so their labels do not line up edge to edge
      pos.set(id, { x: x + sub * SUB_GAP, y: (row - (inSub - 1) / 2) * ROW_GAP + (sub % 2 ? ROW_GAP / 2 : 0) });
    });
    x += (subs - 1) * SUB_GAP + COL_GAP;
  }
  return pos;
}

const FIT_W = 760, FIT_H = 430, FIT_FULL = 0.75;

/** ECharts fits the positions to the chart but draws symbols at their own pixel size (only a user zoom rescales
 *  them), so a graph whose layout will be shrunk a lot to fit draws its nodes smaller by the same factor. 1 for a
 *  graph that fits at about the scale the sizes were chosen for. Estimated for the dialog's usual graph pane. */
export function nodeScale(pos: Map<string, { x: number; y: number }>): number {
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const w = Math.max(...xs) - Math.min(...xs) || 1, h = Math.max(...ys) - Math.min(...ys) || 1;
  return Math.max(0.25, Math.min(1, Math.min(FIT_W / w, FIT_H / h) / FIT_FULL));
}

/** Lineage -> one ECharts graph laid out in kind bands, one legend category per origin. Node `name` is the node id (ECharts links
 *  resolve by name). Fill and icon tell the kind; the ring and legend tell the source system. */
export function toGraphOption(g: Lineage, opts: { highlight?: Set<string> } = {}): EChartsOption {
  const origins = [...sourcesUsed(g).map(({ id, name, hex }) => ({ id, name, hex })),
    ...(g.nodes.some((n) => nodeOrigin(n) === PRISM_ORIGIN) ? [PRISM_ORIGIN] : [])];
  const category = (n: LineageNode) => origins.findIndex((o) => o.id === nodeOrigin(n).id);
  const edgeLabels = g.edges.length <= EDGE_LABEL_MAX;
  const pos = layeredPositions(g);
  const dense = g.nodes.length > DENSE_NODES;
  const scale = nodeScale(pos);
  const rankOf = new Map(g.nodes.map((n) => [n.id, KIND_RANK[n.kind]]));
  return {
    tooltip: { formatter: (p: any) => (p.dataType === "edge" ? escapeHtml(edgeText(p.data.type))
      : `${KIND_LABEL[p.data.kind as LineageKind]}: ${escapeHtml(p.data.fullLabel)}<br/>From ${escapeHtml(p.data.origin)}`) },
    legend: { data: origins.map((o) => o.name), top: 10, right: 12, orient: "vertical", type: "scroll", icon: "circle",
      itemWidth: 12, itemHeight: 12, backgroundColor: "rgba(255,255,255,0.85)", padding: 8, borderRadius: 6,
      textStyle: { fontSize: 12, color: "#3a4357" } },
    series: [{
      // layout "none" with no box size fits the node centres into 80% of the chart at their own aspect ratio (a set
      // left/right/top/bottom would stretch x and y apart and squash the nodes)
      type: "graph", layout: "none", roam: true, draggable: true, scaleLimit: { min: 0.3, max: 6 },
      nodeScaleRatio: 1, // zooming in grows the nodes with the layout, so a shrunken big graph can be read
      left: "center", top: "center", ...(dense ? { labelLayout: { hideOverlap: true } } : {}),
      categories: origins.map((o) => ({ name: o.name, itemStyle: { color: o.hex } })),
      label: { show: true, position: "bottom", distance: 4, fontSize: 12, fontWeight: 500, color: "#1f2a44",
        width: 150, overflow: "break", lineHeight: 14,
        // a name's backing would wash out the small, close nodes of a dense graph
        ...(dense ? {} : { backgroundColor: "rgba(251,252,253,0.85)", padding: [1, 3], borderRadius: 3 }), formatter: (p: any) => shortLabel(p.data.fullLabel) },
      edgeLabel: { show: edgeLabels, fontSize: 10.5, color: "#5a6478", formatter: (p: any) => edgeText(p.data.type) },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 8,
      lineStyle: { color: "#9aa3b5", width: 1.4, curveness: 0.08, opacity: 0.9 },
      emphasis: { focus: "adjacency", lineStyle: { width: 2.5, color: "#5a6478" }, edgeLabel: { show: true } },
      data: g.nodes.map((n) => {
        const highlighted = opts.highlight?.has(n.id) ?? false;
        return { name: n.id, ...pos.get(n.id), fullLabel: n.label, kind: n.kind, origin: nodeOrigin(n).name, highlighted,
          category: category(n), symbol: nodeSymbol(n.kind, nodeColor(n), highlighted),
          symbolSize: Math.round((NODE_SIZE[n.kind] + (highlighted ? 10 : 0)) * scale), itemStyle: { color: nodeColor(n) } };
      }),
      // a link inside one band would run straight through the nodes between its ends, so it bows out instead
      links: g.edges.map((e) => ({ source: e.from, target: e.to, type: e.type,
        ...(rankOf.get(e.from) === rankOf.get(e.to) ? { lineStyle: { curveness: 0.5 } } : {}) })),
    }],
  } as EChartsOption;
}
