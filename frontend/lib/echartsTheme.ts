export const PRISM_THEME = "prism";
export const PALETTE = ["#14213d", "#b3123a", "#3d6fb6", "#e0a526", "#2f8f83", "#7a5195", "#8c8c8c", "#d45087"];

let registered = false;
export function registerPrismTheme(echarts: { registerTheme: (name: string, theme: object) => void }): void {
  if (registered) return;
  echarts.registerTheme(PRISM_THEME, {
    color: PALETTE,
    backgroundColor: "transparent",
    textStyle: { fontFamily: "ui-sans-serif, system-ui, sans-serif", color: "#1f2937" },
    categoryAxis: { axisLine: { lineStyle: { color: "#cbd5e1" } }, axisLabel: { color: "#475569" } },
    valueAxis: { splitLine: { lineStyle: { color: "#e2e8f0" } }, axisLabel: { color: "#475569" } },
  });
  registered = true;
}
