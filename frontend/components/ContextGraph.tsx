"use client";
import * as echarts from "echarts";
import ReactECharts from "echarts-for-react";
import { useMemo } from "react";
import { PRISM_THEME, registerPrismTheme } from "@/lib/echartsTheme";
import { toGraphOption } from "@/lib/graph";
import type { Lineage } from "@/lib/schemas";

registerPrismTheme(echarts, getComputedStyle(document.body).fontFamily);

export function ContextGraph({ lineage, onSelect, height, highlight }:
  { lineage: Lineage; onSelect: (id: string) => void; height: number | string; highlight?: Set<string> }) {
  const option = useMemo(() => toGraphOption(lineage, { highlight }), [lineage, highlight]);
  const onEvents = useMemo(() => ({
    click: (p: { dataType?: string; data?: { name?: string } }) => {
      if (p.dataType === "node" && p.data?.name) onSelect(p.data.name);
    },
  }), [onSelect]);
  return <ReactECharts echarts={echarts} option={option} theme={PRISM_THEME} notMerge onEvents={onEvents}
    style={{ height, width: "100%" }} />;
}
