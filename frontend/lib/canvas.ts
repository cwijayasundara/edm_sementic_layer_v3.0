import type { HandleInfo, Recipe, RunStatus, RunWidget, Widget } from "@/lib/schemas";

export type CanvasItem = { key: string; widget: Widget; info: HandleInfo | null; origin: string; pinned: boolean;
  status: RunStatus; runId?: string };

export type CanvasAction =
  | { type: "add"; item: Omit<CanvasItem, "pinned"> }
  | { type: "remove"; key: string }
  | { type: "togglePin"; key: string }
  | { type: "openDashboard"; dashboardId: string; title: string; widgets: RunWidget[] }
  | { type: "setRun"; turnId: string; runId: string }
  | { type: "clear" };

export const canPin = (i: CanvasItem) => i.status === "ok" && !!i.info?.recipe;

export function canvasReducer(items: CanvasItem[], action: CanvasAction): CanvasItem[] {
  switch (action.type) {
    case "add":
      return items.some((i) => i.key === action.item.key) ? items : [{ ...action.item, pinned: false }, ...items];
    case "remove":
      return items.filter((i) => i.key !== action.key);
    case "togglePin":
      return items.map((i) => (i.key === action.key && canPin(i) ? { ...i, pinned: !i.pinned } : i));
    case "openDashboard": {
      const fresh: CanvasItem[] = action.widgets.map((w, i) => ({ key: `${action.dashboardId}:${i}`,
        widget: w.widget, info: w.handle_info ?? null, origin: action.title, pinned: false, status: w.status }));
      const keys = new Set(fresh.map((f) => f.key));
      return [...fresh, ...items.filter((i) => !keys.has(i.key))];
    }
    case "setRun":
      return items.map((i) => (i.key.startsWith(`${action.turnId}:`) ? { ...i, runId: action.runId } : i));
    case "clear":
      return [];
  }
}

export function pinnedForSave(items: CanvasItem[]): { widget: Widget; recipe: Recipe }[] {
  return items.filter((i) => i.pinned && canPin(i)).map((i) => ({ widget: i.widget, recipe: i.info!.recipe! }));
}
