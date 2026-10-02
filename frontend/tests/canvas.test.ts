import { describe, expect, it } from "vitest";
import { canPin, canvasReducer, pinnedForSave, type CanvasItem } from "@/lib/canvas";

const recipe = { tool: "run_metric" as const, args: { metric_id: "m", dimensions: [], filters: {}, limit: null } };
const widget = { id: "w1", type: "bar" as const, title: "T", handle: "r_aaaaaaaaaaaa", encoding: { x: "a", y: "b" } };
const info = (r: typeof recipe | null) => ({ columns: ["a", "b"], row_count: 1, source: "s", metric_id: "m", recipe: r });

function add(items: CanvasItem[], key: string, r: typeof recipe | null = recipe) {
  return canvasReducer(items, { type: "add", item: { key, widget, info: info(r), origin: "q", status: "ok" } });
}

describe("canvasReducer", () => {
  it("adds newest first, toggles pins and removes", () => {
    let items = add(add([], "a"), "b");
    expect(items.map((i) => i.key)).toEqual(["b", "a"]);
    items = canvasReducer(items, { type: "togglePin", key: "a" });
    expect(items.find((i) => i.key === "a")?.pinned).toBe(true);
    items = canvasReducer(items, { type: "remove", key: "b" });
    expect(items.map((i) => i.key)).toEqual(["a"]);
  });

  it("does not add the same key twice", () => {
    expect(add(add([], "a"), "a")).toHaveLength(1);
  });

  it("only widgets with a recipe and status ok can be pinned; pins without a recipe are ignored", () => {
    let items = add([], "a", null);
    expect(canPin(items[0])).toBe(false);
    items = canvasReducer(items, { type: "togglePin", key: "a" });
    expect(items[0].pinned).toBe(false);
  });

  it("pinnedForSave returns widget and recipe of pinned items in canvas order", () => {
    let items = add(add([], "a"), "b");
    items = canvasReducer(canvasReducer(items, { type: "togglePin", key: "a" }), { type: "togglePin", key: "b" });
    expect(pinnedForSave(items)).toEqual([{ widget, recipe }, { widget, recipe }]);
  });

  it("opening a dashboard puts its widgets first with their run status", () => {
    const items = canvasReducer(add([], "a"), { type: "openDashboard", dashboardId: "d1", title: "Board", widgets: [
      { widget, status: "ok", handle_info: info(recipe) },
      { widget: { ...widget, id: "w2", handle: "" }, status: "not_permitted" },
    ] });
    expect(items.map((i) => [i.key, i.status, i.origin])).toEqual([
      ["d1:w1", "ok", "Board"], ["d1:w2", "not_permitted", "Board"], ["a", "ok", "q"]]);
  });
});
