"use client";
import * as echarts from "echarts";
import ReactECharts from "echarts-for-react";
import { useEffect, useMemo, useRef } from "react";
import { PRISM_THEME, registerPrismTheme } from "@/lib/echartsTheme";
import { nodeOrigin, toGraphOption } from "@/lib/graph";
import type { Lineage } from "@/lib/schemas";

registerPrismTheme(echarts, getComputedStyle(document.body).fontFamily);

/** `focusSource` shows only one origin's nodes by toggling legend categories through dispatchAction, not the option:
 *  a new option would restart the force layout. */
export function ContextGraph({ lineage, onSelect, height, highlight, focusSource }:
  { lineage: Lineage; onSelect: (id: string) => void; height: number | string; highlight?: Set<string>;
    focusSource?: string | null }) {
  const ref = useRef<ReactECharts>(null);
  const option = useMemo(() => toGraphOption(lineage, { highlight }), [lineage, highlight]);
  const onEvents = useMemo(() => ({
    click: (p: { dataType?: string; data?: { name?: string } }) => {
      if (p.dataType === "node" && p.data?.name) onSelect(p.data.name);
    },
  }), [onSelect]);
  useEffect(() => {
    const chart = ref.current?.getEchartsInstance();
    if (!chart) return;
    const focus = focusSource ? lineage.nodes.find((n) => nodeOrigin(n).id === focusSource) : undefined;
    const focusName = focus ? nodeOrigin(focus).name : null;
    for (const name of new Set(lineage.nodes.map((n) => nodeOrigin(n).name))) {
      chart.dispatchAction({ type: !focusName || name === focusName ? "legendSelect" : "legendUnSelect", name });
    }
  }, [focusSource, lineage, option]);
  return <ReactECharts ref={ref} echarts={echarts} option={option} theme={PRISM_THEME} notMerge onEvents={onEvents}
    style={{ height, width: "100%" }} />;
}
