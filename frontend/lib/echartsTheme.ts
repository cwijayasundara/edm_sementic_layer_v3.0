export const PRISM_THEME = "prism";
export const PALETTE = ["#14213d", "#b3123a", "#3d6fb6", "#e0a526", "#2f8f83", "#7a5195", "#8c8c8c", "#d45087"];

let registered = false;
// fontFamily: the page's resolved font (next/font hashes the family name, and canvas text cannot read CSS variables)
export function registerPrismTheme(echarts: { registerTheme: (name: string, theme: object) => void },
  fontFamily = "ui-sans-serif, system-ui, sans-serif"): void {
  if (registered) return;
  echarts.registerTheme(PRISM_THEME, {
    color: PALETTE,
    backgroundColor: "transparent",
    textStyle: { fontFamily, color: "#172033" },
    legend: { textStyle: { color: "#5a6478" }, itemWidth: 10, itemHeight: 10, icon: "roundRect" },
    tooltip: { backgroundColor: "#14213d", borderWidth: 0, textStyle: { color: "#ffffff", fontFamily },
      extraCssText: "border-radius:6px;box-shadow:0 8px 24px -6px rgba(20,33,61,.4);" },
    categoryAxis: { axisLine: { lineStyle: { color: "#c3cad6" } }, axisTick: { show: false }, axisLabel: { color: "#5a6478" } },
    bar: { barMaxWidth: 44, itemStyle: { borderRadius: [3, 3, 0, 0] } },
    valueAxis: { splitLine: { lineStyle: { color: "#e8ebf0" } }, axisLine: { show: false }, axisLabel: { color: "#5a6478" } },
  });
  registered = true;
}
