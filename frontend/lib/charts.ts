/* eslint-disable @typescript-eslint/no-explicit-any */
import type { EChartsOption } from "echarts";
import { formatValue } from "@/lib/format";
import type { Widget } from "@/lib/schemas";

export class ChartError extends Error {}

function index(columns: string[], name: string | null | undefined, field: string): number {
  if (!name) throw new ChartError(`missing encoding.${field}`);
  const i = columns.indexOf(name);
  if (i < 0) throw new ChartError(`column ${name} is not in the result`);
  return i;
}

const toNum = (v: unknown): number | null => (typeof v === "number" ? v : v == null || v === "" ? null : Number.isFinite(Number(v)) ? Number(v) : null);
const label = (v: unknown) => (v == null ? "(none)" : String(v));

function uniq(values: string[]): string[] {
  return [...new Set(values)];
}

/** Pivot rows into categories x series, summing duplicate cells; a missing cell is null. */
function pivot(rows: unknown[][], xi: number, yi: number, si: number | null) {
  const cats = uniq(rows.map((r) => label(r[xi])));
  const names = si === null ? ["value"] : uniq(rows.map((r) => label(r[si])));
  const cells = new Map<string, number>();
  for (const r of rows) {
    const key = `${si === null ? "value" : label(r[si])}\u0000${label(r[xi])}`;
    const n = toNum(r[yi]);
    if (n !== null) cells.set(key, (cells.get(key) ?? 0) + n);
  }
  const series = names.map((name) => ({ name, data: cats.map((c) => cells.get(`${name}\u0000${c}`) ?? null) }));
  return { cats, series };
}

export function toEChartsOption(widget: Widget, columns: string[], rows: unknown[][]): EChartsOption | null {
  const e = widget.encoding;
  const unit = e.unit ?? null;
  const tooltipFmt = (v: unknown) => formatValue(typeof v === "number" ? v : toNum(v), unit);
  switch (widget.type) {
    case "kpi":
    case "table":
      return null;
    case "bar":
    case "stacked_bar":
    case "line": {
      const xi = index(columns, e.x, "x"), yi = index(columns, e.y, "y");
      const si = e.series ? index(columns, e.series, "series") : null;
      const { cats, series } = pivot(rows, xi, yi, si);
      const type = widget.type === "line" ? "line" : "bar";
      return {
        tooltip: { trigger: "axis", valueFormatter: tooltipFmt },
        legend: si === null ? undefined : { data: series.map((s) => s.name), top: 0 },
        grid: { left: 48, right: 16, top: si === null ? 16 : 32, bottom: 40, containLabel: true },
        xAxis: { type: "category", data: cats },
        yAxis: { type: "value", name: unit ?? undefined },
        series: series.map((s) => ({ name: s.name, type, data: s.data,
          ...(widget.type === "stacked_bar" ? { stack: "total" } : {}) })),
      } as EChartsOption;
    }
    case "pie": {
      const xi = index(columns, e.x, "x"), yi = index(columns, e.y, "y");
      const { cats, series } = pivot(rows, xi, yi, null);
      return {
        tooltip: { trigger: "item", valueFormatter: tooltipFmt },
        legend: { type: "scroll", bottom: 0 },
        series: [{ type: "pie", radius: ["40%", "70%"], data: cats.map((name, i) => ({ name, value: series[0].data[i] })) }],
      } as EChartsOption;
    }
    case "scatter": {
      const xi = index(columns, e.x, "x"), yi = index(columns, e.y, "y");
      return {
        tooltip: { trigger: "item", formatter: (params: any) => {
          const [x, y] = params.value;
          return `${x}, ${formatValue(y, unit)}`;
        }},
        grid: { left: 48, right: 16, top: 16, bottom: 40, containLabel: true },
        xAxis: { type: "value", name: e.x ?? undefined },
        yAxis: { type: "value", name: unit ?? e.y ?? undefined },
        series: [{ type: "scatter", data: rows.map((r) => [toNum(r[xi]), toNum(r[yi])]) }],
      } as EChartsOption;
    }
    case "heatmap": {
      const xi = index(columns, e.x, "x"), yi = index(columns, e.y, "y"), vi = index(columns, e.value, "value");
      const xs = uniq(rows.map((r) => label(r[xi]))), ys = uniq(rows.map((r) => label(r[yi])));
      const data = rows.map((r) => [xs.indexOf(label(r[xi])), ys.indexOf(label(r[yi])), toNum(r[vi])]);
      const values = data.map((d) => d[2]).filter((v): v is number => v !== null);
      return {
        tooltip: { position: "top", formatter: (params: any) => {
          const value = params.value[2];
          return formatValue(value, unit);
        }},
        grid: { left: 48, right: 16, top: 16, bottom: 64, containLabel: true },
        xAxis: { type: "category", data: xs, splitArea: { show: true } },
        yAxis: { type: "category", data: ys, splitArea: { show: true } },
        visualMap: { min: values.length ? Math.min(...values) : 0, max: values.length ? Math.max(...values) : 0,
          calculable: true, orient: "horizontal", left: "center", bottom: 0 },
        series: [{ type: "heatmap", data, label: { show: false } }],
      } as EChartsOption;
    }
  }
}

export function kpiValue(widget: Widget, columns: string[], rows: unknown[][]): unknown {
  const i = index(columns, widget.encoding.value, "value");
  return rows.length ? rows[0][i] : null;
}
