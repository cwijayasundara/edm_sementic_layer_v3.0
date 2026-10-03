/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, it } from "vitest";
import { DENSE_NODES, EDGE_LABEL_MAX, KIND_RANK, KIND_STYLE, NODE_SIZE, layeredPositions, nodeScale, PRISM_ORIGIN, edgeText, nodeColor, nodeOrigin, nodeSvg, nodeSymbol,
  originText, shortLabel, sourcesUsed, toGraphOption } from "@/lib/graph";
import { Lineage } from "@/lib/schemas";
import { SOURCES } from "@/lib/sources";

const g = Lineage.parse({
  nodes: [
    { id: "metric:open_breaks", kind: "Metric", label: "open_breaks", source: "cashrecon", detail: "Open breaks" },
    { id: "source:cashrecon", kind: "Source", label: "cashrecon" },
    { id: "column:cashrecon.breaks.region", kind: "Column", label: "breaks.region", source: "cashrecon" },
    { id: "question:q1", kind: "Question", label: "Which legal entity has the most USD breaks open longer than 5 days?" },
  ],
  edges: [{ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" }],
  truncated: false, governed: true,
});

describe("toGraphOption", () => {
  it("one fixed-layout graph, nodes named by id, sized and drawn by kind with their origin's ring, categorised by origin", () => {
    const s = (toGraphOption(g) as any).series[0];
    expect(s.type).toBe("graph");
    expect(s.layout).toBe("none");
    for (const d of s.data) expect(d).toMatchObject(layeredPositions(g).get(d.name)!);
    expect(s.data.map((d: any) => d.name)).toEqual(g.nodes.map((n) => n.id));
    expect(s.data[0].symbolSize).toBe(NODE_SIZE.Metric);
    expect(s.data.map((d: any) => d.symbol)).toEqual(g.nodes.map((n) => nodeSymbol(n.kind, nodeColor(n))));
    expect(s.categories.map((c: any) => c.name)).toEqual(["CashRecon", "Prism knowledge"]);
    expect(s.data.map((d: any) => d.category)).toEqual([0, 0, 0, 1]);
    expect((toGraphOption(g) as any).legend.data).toEqual(["CashRecon", "Prism knowledge"]);
    expect(s.links).toEqual([{ source: "source:cashrecon", target: "metric:open_breaks", type: "PROVIDES" }]);
  });

  it("legend swatches match the colour of their origin's nodes", () => {
    const s = (toGraphOption(g) as any).series[0];
    for (const [i, c] of s.categories.entries()) {
      const nodes = s.data.filter((d: any) => d.category === i);
      expect(nodes.length).toBeGreaterThan(0);
      for (const d of nodes) expect(c.itemStyle.color).toBe(d.itemStyle.color);
    }
  });

  it("labels show a short form and tooltips the full label or the edge type in words", () => {
    const o = toGraphOption(g) as any;
    const q = o.series[0].data[3];
    expect(o.series[0].label.formatter({ data: q })).toBe(shortLabel(q.fullLabel));
    expect(o.tooltip.formatter({ dataType: "node", data: q })).toContain("Past question: Which legal entity");
    expect(o.tooltip.formatter({ dataType: "node", data: o.series[0].data[2] })).toContain("From CashRecon");
    expect(o.tooltip.formatter({ dataType: "node", data: { ...q, fullLabel: "<b>x</b>" } })).toContain("&lt;b&gt;x");
    expect(o.tooltip.formatter({ dataType: "edge", data: { type: "HAS_DIMENSION" } })).toBe("has dimension");
    expect(o.series[0].edgeLabel.formatter({ data: { type: "COMPUTED_FROM" } })).toBe("computed from");
  });

  it("haloes highlighted nodes in gold", () => {
    const s = (toGraphOption(g, { highlight: new Set(["metric:open_breaks"]) }) as any).series[0];
    expect(s.data[0].highlighted).toBe(true);
    expect(s.data[0].symbol).toBe(nodeSymbol("Metric", "#2f8f83", true));
    expect(decodeURIComponent(s.data[0].symbol)).toContain('stroke="#e0a526"');
    expect(s.data[0].symbolSize).toBeGreaterThan(NODE_SIZE.Metric);
    expect(s.data[1].highlighted).toBe(false);
    expect(decodeURIComponent(s.data[1].symbol)).not.toContain("#e0a526");
  });

  it("edge names show on the lines until the graph is too busy, then only on hover", () => {
    expect((toGraphOption(g) as any).series[0].edgeLabel.show).toBe(true);
    const busy = Lineage.parse({ ...g, edges: Array.from({ length: EDGE_LABEL_MAX + 1 },
      () => ({ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" })) });
    const s = (toGraphOption(busy) as any).series[0];
    expect(s.edgeLabel.show).toBe(false);
    expect(s.emphasis.edgeLabel.show).toBe(true);
    expect(edgeText("ANSWERED_BY")).toBe("answered by");
  });

  it("a node image is the kind's fill and name inside a ring of the source colour", () => {
    const svg = nodeSvg("Table", "#d08a1c");
    expect(svg).toContain(`fill="${KIND_STYLE.Table.fill}"`);
    expect(svg).toContain('fill="#d08a1c"');
    expect(svg).toContain(">Table</text>");
    const fills = new Set(Object.values(KIND_STYLE).map((k) => k.fill));
    expect(fills.size).toBe(Object.keys(KIND_STYLE).length);
  });

  it("no kind fill can be mistaken for a source system's ring or legend colour", () => {
    const rgb = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const dist = (a: string, b: string) => Math.hypot(...rgb(a).map((v, i) => v - rgb(b)[i]));
    for (const [kind, { fill }] of Object.entries(KIND_STYLE)) {
      if (kind === "Result") continue; // Prism's own: navy fill inside a navy ring
      for (const s of SOURCES) expect(dist(fill, s.hex), `${kind} vs ${s.name}`).toBeGreaterThan(60);
    }
  });
});

describe("layeredPositions", () => {
  const many = (n: number) => Lineage.parse({ truncated: false, governed: true, edges: [],
    nodes: Array.from({ length: n }, (_, i) => ({ id: `column:c${i}`, kind: "Column", label: `c${i}`, source: "cashrecon" })) });

  it("bands run left to right in kind order", () => {
    const pos = layeredPositions(g);
    const x = (id: string) => pos.get(id)!.x;
    expect(x("question:q1")).toBeLessThan(x("metric:open_breaks"));
    expect(x("metric:open_breaks")).toBeLessThan(x("source:cashrecon"));
    expect(x("source:cashrecon")).toBeLessThan(x("column:cashrecon.breaks.region"));
    expect(KIND_RANK.Table).toBeLessThan(KIND_RANK.Column);
  });

  it("no two nodes share a place, even when a band wraps", () => {
    const pos = layeredPositions(many(60));
    expect(new Set([...pos.values()].map((p) => `${p.x},${p.y}`)).size).toBe(60);
    expect(new Set([...pos.values()].map((p) => p.x)).size).toBeGreaterThan(1);
  });

  it("is the same every time for the same lineage", () => {
    expect([...layeredPositions(g)]).toEqual([...layeredPositions(g)]);
  });

  it("orders a band by its neighbours so lines cross less", () => {
    const x = Lineage.parse({ truncated: false, governed: true, nodes: [
      { id: "metric:a", kind: "Metric", label: "a" }, { id: "metric:b", kind: "Metric", label: "b" },
      { id: "table:b", kind: "Table", label: "tb" }, { id: "table:a", kind: "Table", label: "ta" },
    ], edges: [{ from: "metric:a", to: "table:a", type: "COMPUTED_FROM" }, { from: "metric:b", to: "table:b", type: "COMPUTED_FROM" }] });
    const pos = layeredPositions(x);
    expect(pos.get("table:a")!.y).toBeLessThan(pos.get("table:b")!.y);
  });

  it("a big graph draws smaller nodes and hides colliding names; a small one does not", () => {
    expect(nodeScale(layeredPositions(g))).toBe(1);
    expect(nodeScale(layeredPositions(many(150)))).toBeLessThan(1);
    const big = (toGraphOption(many(150)) as any).series[0];
    expect(big.data[0].symbolSize).toBeLessThan(NODE_SIZE.Column);
    expect((toGraphOption(g) as any).series[0].labelLayout).toBeUndefined();
    expect((toGraphOption(many(DENSE_NODES + 1)) as any).series[0].labelLayout).toEqual({ hideOverlap: true });
  });

  it("a link inside one band bows out instead of running through the band", () => {
    const x = Lineage.parse({ truncated: false, governed: true, nodes: [
      { id: "metric:a", kind: "Metric", label: "a" }, { id: "metric:b", kind: "Metric", label: "b" },
      { id: "source:s", kind: "Source", label: "s" },
    ], edges: [{ from: "metric:a", to: "metric:b", type: "JOINABLE_ON" }, { from: "source:s", to: "metric:a", type: "PROVIDES" }] });
    const links = (toGraphOption(x) as any).series[0].links;
    expect(links[0].lineStyle.curveness).toBe(0.5);
    expect(links[1].lineStyle).toBeUndefined();
  });
});

describe("node origin", () => {
  it("every node of a source wears its colour; result, terms and questions are Prism's own", () => {
    for (const n of g.nodes.slice(0, 3)) expect(nodeColor(n)).toBe("#2f8f83");
    expect(nodeOrigin(g.nodes[1]).name).toBe("CashRecon");
    expect(nodeOrigin(g.nodes[3])).toBe(PRISM_ORIGIN);
    expect(nodeOrigin({ id: "result:combined", kind: "Result", label: "Combined result" })).toBe(PRISM_ORIGIN);
    expect(nodeOrigin({ id: "t", kind: "BusinessTerm", label: "t" })).toBe(PRISM_ORIGIN);
    const sourceHexes = ["#3d6fb6", "#7a5195", "#2f8f83", "#d08a1c", "#c2416b"];
    expect(sourceHexes).not.toContain(PRISM_ORIGIN.hex);
  });

  it("an unknown source keeps its own name and a category", () => {
    const x = Lineage.parse({ ...g, nodes: [{ id: "table:edm9.t", kind: "Table", label: "edm9.t", source: "edm9" }] });
    expect(nodeOrigin(x.nodes[0]).name).toBe("edm9");
    expect((toGraphOption(x) as any).series[0].data[0].category).toBe(0);
  });

  it("sourcesUsed lists each system once with how it was reached and its node count", () => {
    const multi = Lineage.parse({ nodes: [
      { id: "metric:m", kind: "Metric", label: "m", source: "marketmaster" },
      { id: "table:marketmaster.p", kind: "Table", label: "marketmaster.p", source: "marketmaster" },
      { id: "endpoint:e", kind: "Endpoint", label: "e", source: "marketmaster" },
      { id: "column:refmaster.s.isin", kind: "Column", label: "s.isin", source: "refmaster" },
      { id: "metric:open_breaks", kind: "Metric", label: "open_breaks", source: "cashrecon" },
      { id: "result:combined", kind: "Result", label: "Combined result" },
    ], edges: [], truncated: false, governed: true });
    expect(sourcesUsed(multi)).toEqual([
      { id: "refmaster", name: "RefMaster", hex: "#3d6fb6", access: ["SQL database"], count: 1 },
      { id: "marketmaster", name: "MarketMaster", hex: "#7a5195", access: ["REST API", "SQL database"], count: 3 },
      { id: "cashrecon", name: "CashRecon", hex: "#2f8f83", access: ["SQL database"], count: 1 },
    ]);
    const uses = sourcesUsed(multi);
    expect(originText(multi.nodes[1], uses)).toBe("From MarketMaster · SQL database");
    expect(originText(multi.nodes[0], uses)).toBe("From MarketMaster · REST API + SQL database");
    expect(originText(multi.nodes[5], uses)).toBe("From Prism knowledge");
  });

  it("shortLabel truncates with an ellipsis", () => {
    expect(shortLabel("open_breaks")).toBe("open_breaks");
    expect(shortLabel("x".repeat(60))).toHaveLength(40);
    expect(shortLabel("x".repeat(60)).endsWith("…")).toBe(true);
  });
});

describe("Lineage schema", () => {
  it("rejects an unknown node kind", () => {
    expect(() => Lineage.parse({ ...g, nodes: [{ id: "role:x", kind: "Role", label: "x" }] })).toThrow();
  });
});
