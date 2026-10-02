import { describe, expect, it } from "vitest";
import { Trace } from "@/lib/schemas";
import { formatMs, stepRows, touchedIds } from "@/lib/trace";

const T = Trace.parse({ run_id: "a".repeat(32), question: "q", answer: "a", path: "metric", status: "ok",
  confirmed: true, created_at: 1, steps: [
    { seq: 0, parent: null, kind: "delegate", label: "Delegated to cashrecon: x", note: null, considered: [], ms: 900,
      status: "ok", error_code: null, tool: "delegate", args: null, handle: null, rows: null, truncated: null, touched: [] },
    { seq: 1, parent: 0, kind: "metric", label: "Ran metric open_breaks", note: "first", considered: [], ms: 40,
      status: "ok", error_code: null, tool: "run_metric", args: "{\"metric_id\":\"open_breaks\"}",
      handle: "r_aaaaaaaaaaaa", rows: 12, truncated: false,
      touched: [{ id: "metric:open_breaks", kind: "Metric", label: "open_breaks" }] },
    { seq: 2, parent: null, kind: "answer", label: "Answered", note: null, considered: [], ms: null, status: "ok",
      error_code: null, tool: null, args: null, handle: null, rows: null, truncated: null, touched: [] },
  ] });

describe("trace helpers", () => {
  it("orders children under their delegate step", () => {
    expect(stepRows(T).map((r) => [r.step.seq, r.depth])).toEqual([[0, 0], [1, 1], [2, 0]]);
  });
  it("collects touched node ids", () => {
    expect([...touchedIds(T)]).toEqual(["metric:open_breaks"]);
  });
  it("formats durations", () => {
    expect([formatMs(40), formatMs(1250), formatMs(null)]).toEqual(["40 ms", "1.3 s", ""]);
  });
});
