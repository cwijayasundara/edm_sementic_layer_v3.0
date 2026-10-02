import { describe, expect, it } from "vitest";
import { ChartError, kpiValue, toEChartsOption } from "@/lib/charts";
import type { Widget } from "@/lib/schemas";

const w = (type: Widget["type"], encoding: Widget["encoding"]): Widget =>
  ({ id: "w1", type, title: "T", handle: "r_aaaaaaaaaaaa", encoding });
const cols = ["region", "status", "value"];
const rows = [["EMEA", "open", 3], ["APAC", "open", 2], ["EMEA", "aged", 1]];

describe("toEChartsOption", () => {
  it("bar: categories in first-seen order, one series, duplicates summed", () => {
    const o = toEChartsOption(w("bar", { x: "region", y: "value" }), cols, rows) as any;
    expect(o.xAxis.data).toEqual(["EMEA", "APAC"]);
    expect(o.series).toEqual([expect.objectContaining({ type: "bar", data: [4, 2] })]);
  });

  it("stacked_bar pivots by series and stacks", () => {
    const o = toEChartsOption(w("stacked_bar", { x: "region", y: "value", series: "status" }), cols, rows) as any;
    expect(o.series.map((s: any) => [s.name, s.stack, s.data])).toEqual([["open", "total", [3, 2]], ["aged", "total", [1, null]]]);
    expect(o.legend.data).toEqual(["open", "aged"]);
  });

  it("line keeps row order on a category axis", () => {
    const o = toEChartsOption(w("line", { x: "region", y: "value" }), cols, rows) as any;
    expect(o.series[0].type).toBe("line") ;
    expect(o.xAxis.type).toBe("category");
  });

  it("pie uses x as name and y as value", () => {
    const o = toEChartsOption(w("pie", { x: "region", y: "value" }), cols, rows) as any;
    expect(o.series[0].data).toEqual([{ name: "EMEA", value: 4 }, { name: "APAC", value: 2 }]);
  });

  it("scatter uses numeric axes", () => {
    const o = toEChartsOption(w("scatter", { x: "a", y: "b" }), ["a", "b"], [[1, 2], [3, 4]]) as any;
    expect(o.xAxis.type).toBe("value");
    expect(o.series[0].data).toEqual([[1, 2], [3, 4]]);
  });

  it("heatmap maps x/y categories and a visualMap over the value range", () => {
    const o = toEChartsOption(w("heatmap", { x: "region", y: "status", value: "value" }), cols, rows) as any;
    expect(o.xAxis.data).toEqual(["EMEA", "APAC"]);
    expect(o.yAxis.data).toEqual(["open", "aged"]);
    expect(o.series[0].data).toEqual([[0, 0, 3], [1, 0, 2], [0, 1, 1]]);
    expect([o.visualMap.min, o.visualMap.max]).toEqual([1, 3]);
  });

  it("puts the unit on the value axis name", () => {
    const o = toEChartsOption(w("bar", { x: "region", y: "value", unit: "USD" }), cols, rows) as any;
    expect(o.yAxis.name).toBe("USD");
  });

  it("returns null for kpi and table, and throws ChartError for a missing column", () => {
    expect(toEChartsOption(w("table", {}), cols, rows)).toBeNull();
    expect(toEChartsOption(w("kpi", { value: "value" }), cols, rows)).toBeNull();
    expect(() => toEChartsOption(w("bar", { x: "desk", y: "value" }), cols, rows)).toThrow(ChartError);
  });

  it("scatter tooltip formats y-value with unit", () => {
    const o = toEChartsOption(w("scatter", { x: "a", y: "b", unit: "USD" }), ["a", "b"], [[1, 1200], [3, 4000]]) as any;
    const formatter = o.tooltip.formatter;
    expect(formatter({ value: [1, 1200] })).toContain("$1.2K");
  });

  it("heatmap tooltip formats value with unit (% for percentage)", () => {
    const o = toEChartsOption(w("heatmap", { x: "region", y: "status", value: "value", unit: "%" }), cols, rows) as any;
    const formatter = o.tooltip.formatter;
    expect(formatter({ value: [0, 0, 3] })).toBe("3.0%");
  });
});

describe("kpiValue", () => {
  it("reads the value column of the first row", () => {
    expect(kpiValue(w("kpi", { value: "value" }), cols, rows)).toBe(3);
    expect(kpiValue(w("kpi", { value: "value" }), cols, [])).toBeNull();
  });
});
