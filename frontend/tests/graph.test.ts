/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, it } from "vitest";
import { NODE_SIZE, nodeColor, shortLabel, toGraphOption } from "@/lib/graph";
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
  it("one force graph, nodes named by id, sized and categorised by kind", () => {
    const s = (toGraphOption(g) as any).series[0];
    expect(s.type).toBe("graph");
    expect(s.layout).toBe("force");
    expect(s.data.map((d: any) => d.name)).toEqual(g.nodes.map((n) => n.id));
    expect(s.data[0].symbolSize).toBe(NODE_SIZE.Metric);
    expect(s.categories.map((c: any) => c.name)).toEqual(["Metric", "Source", "Column", "Past question"]);
    expect(s.links).toEqual([{ source: "source:cashrecon", target: "metric:open_breaks", type: "PROVIDES" }]);
  });

  it("labels show a short form and tooltips the full label or the edge type", () => {
    const o = toGraphOption(g) as any;
    const q = o.series[0].data[3];
    expect(o.series[0].label.formatter({ data: q })).toBe(shortLabel(q.fullLabel));
    expect(o.tooltip.formatter({ dataType: "node", data: q })).toContain("Past question: Which legal entity");
    expect(o.tooltip.formatter({ dataType: "edge", data: { type: "PROVIDES" } })).toBe("PROVIDES");
  });
});

describe("node styling", () => {
  it("metric navy, source in its colour, column a lighter tint, question orange", () => {
    expect(nodeColor(g.nodes[0])).toBe("#14213d");
    expect(nodeColor(g.nodes[1])).toBe("#2f8f83");
    expect(nodeColor(g.nodes[2])).not.toBe("#2f8f83");
    expect(nodeColor(g.nodes[3])).toBe("#d08a1c");
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
