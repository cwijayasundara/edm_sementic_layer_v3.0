"use client";
import * as echarts from "echarts";
import ReactECharts from "echarts-for-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { PRISM_THEME, registerPrismTheme } from "@/lib/echartsTheme";
import { DEFAULT_VIEWPORT, type Viewport, nodeOrigin, toGraphOption } from "@/lib/graph";
import type { Lineage } from "@/lib/schemas";

registerPrismTheme(echarts, getComputedStyle(document.body).fontFamily);

/** `focusSource` shows only one origin's nodes by toggling legend categories through dispatchAction, not the option:
 *  a new option would put back nodes the user has dragged. */
export function ContextGraph({ lineage, onSelect, height, highlight, focusSource }:
  { lineage: Lineage; onSelect: (id: string) => void; height: number | string; highlight?: Set<string>;
    focusSource?: string | null }) {
  const ref = useRef<ReactECharts>(null);
  const box = useRef<HTMLDivElement>(null);
  const viewport = useViewport(box);
  const option = useMemo(() => toGraphOption(lineage, { highlight, viewport }), [lineage, highlight, viewport]);
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
  return <div ref={box} style={{ height, width: "100%" }}>
    <ReactECharts ref={ref} echarts={echarts} option={option} theme={PRISM_THEME} notMerge onEvents={onEvents}
      style={{ height: "100%", width: "100%" }} /></div>;
}

/** The chart's size, so node sizes suit the pane (a phone draws much smaller nodes than a desktop). Rounded to 100px:
 *  every change is a new option, which puts back dragged nodes, so small resizes are ignored. */
function useViewport(box: React.RefObject<HTMLDivElement | null>): Viewport {
  const [viewport, setViewport] = useState<Viewport>(DEFAULT_VIEWPORT);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const round = (n: number) => Math.max(100, Math.round(n / 100) * 100);
    const measure = () => {
      const width = round(el.clientWidth), height = round(el.clientHeight);
      setViewport((v) => (v.width === width && v.height === height ? v : { width, height }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [box]);
  return viewport;
}
