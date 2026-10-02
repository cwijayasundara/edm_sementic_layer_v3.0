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
      ["d1:0", "ok", "Board"], ["d1:1", "not_permitted", "Board"], ["a", "ok", "q"]]);
  });

  it("keys reopened cards by position so widgets with the same per-turn id stay distinct", () => {
    const widgets = [
      { widget, status: "ok" as const, handle_info: info(recipe) },
      { widget: { ...widget, handle: "r_bbbbbbbbbbbb" }, status: "ok" as const, handle_info: info(recipe) },
    ];
    const open = (items: CanvasItem[]) => canvasReducer(items, { type: "openDashboard", dashboardId: "d1", title: "Board", widgets });
    const once = open([]);
    expect(once.map((i) => i.key)).toEqual(["d1:0", "d1:1"]);
    expect(once.map((i) => i.widget.handle)).toEqual(["r_aaaaaaaaaaaa", "r_bbbbbbbbbbbb"]);
    const twice = open(once);
    expect(twice.map((i) => i.key)).toEqual(["d1:0", "d1:1"]);
    const pinned = canvasReducer(twice, { type: "togglePin", key: "d1:0" });
    expect(pinned.map((i) => i.pinned)).toEqual([true, false]);
  });

  it("setRun attaches the run id to the turn's items only", () => {
    let items = add(add([], "t1:w1"), "t2:w1");
    items = canvasReducer(items, { type: "setRun", turnId: "t1", runId: "a".repeat(32) });
    expect(items.find((i) => i.key === "t1:w1")!.runId).toBe("a".repeat(32));
    expect(items.find((i) => i.key === "t2:w1")!.runId).toBeUndefined();
  });
});
