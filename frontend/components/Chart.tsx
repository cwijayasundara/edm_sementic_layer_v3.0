"use client";
import * as echarts from "echarts";
import type { EChartsOption } from "echarts";
import ReactECharts from "echarts-for-react";
import { PRISM_THEME, registerPrismTheme } from "@/lib/echartsTheme";

registerPrismTheme(echarts, getComputedStyle(document.body).fontFamily);

export function Chart({ option, height }: { option: EChartsOption; height: number }) {
  return <ReactECharts echarts={echarts} option={option} theme={PRISM_THEME} notMerge style={{ height, width: "100%" }} />;
}
