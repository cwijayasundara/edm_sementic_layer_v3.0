/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, it } from "vitest";
import { NODE_SHAPE, NODE_SIZE, PRISM_ORIGIN, nodeColor, nodeOrigin, originText, shortLabel, sourcesUsed,
  toGraphOption } from "@/lib/graph";
import { Lineage } from "@/lib/schemas";

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
  it("one force graph, nodes named by id, sized and shaped by kind, categorised by origin", () => {
    const s = (toGraphOption(g) as any).series[0];
    expect(s.type).toBe("graph");
    expect(s.layout).toBe("force");
    expect(s.data.map((d: any) => d.name)).toEqual(g.nodes.map((n) => n.id));
    expect(s.data[0].symbolSize).toBe(NODE_SIZE.Metric);
    expect(s.data.map((d: any) => d.symbol)).toEqual(g.nodes.map((n) => NODE_SHAPE[n.kind]));
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

  it("labels show a short form and tooltips the full label or the edge type", () => {
    const o = toGraphOption(g) as any;
    const q = o.series[0].data[3];
    expect(o.series[0].label.formatter({ data: q })).toBe(shortLabel(q.fullLabel));
    expect(o.tooltip.formatter({ dataType: "node", data: q })).toContain("Past question: Which legal entity");
    expect(o.tooltip.formatter({ dataType: "node", data: o.series[0].data[2] })).toContain("From CashRecon");
    expect(o.tooltip.formatter({ dataType: "node", data: { ...q, fullLabel: "<b>x</b>" } })).toContain("&lt;b&gt;x");
    expect(o.tooltip.formatter({ dataType: "edge", data: { type: "PROVIDES" } })).toBe("PROVIDES");
  });

  it("rings highlighted nodes", () => {
    const s = (toGraphOption(g, { highlight: new Set(["metric:open_breaks"]) }) as any).series[0];
    expect(s.data[0].itemStyle.borderWidth).toBe(3);
    expect(s.data[0].itemStyle.borderColor).toBe("#e0a526");
    expect(s.data[1].itemStyle.borderWidth).toBeUndefined();
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
    expect(shortLabel("x".repeat(40))).toHaveLength(18);
    expect(shortLabel("x".repeat(40)).endsWith("…")).toBe(true);
  });
});

describe("Lineage schema", () => {
  it("rejects an unknown node kind", () => {
    expect(() => Lineage.parse({ ...g, nodes: [{ id: "role:x", kind: "Role", label: "x" }] })).toThrow();
  });
});
