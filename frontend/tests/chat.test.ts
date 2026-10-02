import { describe, expect, it } from "vitest";
import { NO_RESPONSE, chatReducer, widgetKey, type Turn } from "@/lib/chat";

const widgetEvent = { type: "widget" as const, widget: { id: "w1", type: "bar" as const, title: "T",
  handle: "r_aaaaaaaaaaaa", encoding: { x: "region", y: "value" } },
  handle_info: { columns: ["region", "value"], row_count: 1, source: "cashrecon", metric_id: "open_breaks" } };
const telemetry = { type: "telemetry" as const, run_id: "r", path: "metric", models: [], input_tokens: 1,
  output_tokens: 1, cache_read_input_tokens: 0, llm_turns: 1, tool_calls: 1, tool_latency_ms: 1, cost_usd: 0.001 };

function run(...actions: Parameters<typeof chatReducer>[1][]): Turn[] {
  return actions.reduce(chatReducer, [] as Turn[]);
}

describe("chatReducer", () => {
  it("collects plan, widgets, summary and finishes on telemetry", () => {
    const [t] = run(
      { type: "start", id: "t1", question: "q" },
      { type: "event", id: "t1", event: { type: "plan", tool: "run_metric", label: "metric open_breaks" } },
      { type: "event", id: "t1", event: widgetEvent },
      { type: "event", id: "t1", event: { type: "summary", text: "EMEA leads." } },
      { type: "event", id: "t1", event: telemetry },
    );
    expect(t).toMatchObject({ status: "done", summary: "EMEA leads.", widgetKeys: [widgetKey("t1", "w1")] });
    expect(t.plan).toHaveLength(1);
    expect(t.telemetry?.cost_usd).toBe(0.001);
  });

  it("an error event marks the turn failed with the agent's fixed message", () => {
    const [t] = run({ type: "start", id: "t1", question: "q" },
      { type: "event", id: "t1", event: { type: "error", code: "run_limit", message: "Try a narrower question." } },
      { type: "event", id: "t1", event: telemetry });
    expect(t).toMatchObject({ status: "error", error: "Try a narrower question." });
  });

  it("a stream that ends without telemetry is done if it has a summary, else an error", () => {
    const [a] = run({ type: "start", id: "a", question: "q" },
      { type: "event", id: "a", event: { type: "summary", text: "s" } }, { type: "ended", id: "a" });
    expect(a.status).toBe("done");
    const [b] = run({ type: "start", id: "b", question: "q" }, { type: "ended", id: "b" });
    expect(b).toMatchObject({ status: "error", error: NO_RESPONSE });
  });

  it("ignores events for a stopped turn and keeps the next turn independent", () => {
    const turns = run(
      { type: "start", id: "t1", question: "first" },
      { type: "stop", id: "t1" },
      { type: "event", id: "t1", event: { type: "summary", text: "late" } },
      { type: "start", id: "t2", question: "second" },
      { type: "event", id: "t2", event: { type: "summary", text: "fresh" } },
    );
    expect(turns.map((t) => [t.id, t.status, t.summary])).toEqual([["t1", "stopped", undefined], ["t2", "streaming", "fresh"]]);
  });
});

describe("confirm", () => {
  it("keeps the answer and applies confirm actions even after the turn is done", () => {
    const rid = "0b8f3c1e-2d4a-4c6b-9e7f-1a2b3c4d5e6f";
    let s = chatReducer([], { type: "start", id: "t1", question: "q" });
    s = chatReducer(s, { type: "event", id: "t1", event: { type: "summary", text: "x" } });
    s = chatReducer(s, { type: "event", id: "t1", event: { type: "answer", record_id: rid, confirmable: true } });
    s = chatReducer(s, { type: "ended", id: "t1" });
    expect(s[0].status).toBe("done");
    expect(s[0].answer).toEqual({ recordId: rid, confirmable: true, state: "idle" });
    s = chatReducer(s, { type: "confirm", id: "t1", state: "sending" });
    s = chatReducer(s, { type: "confirm", id: "t1", state: "confirmed" });
    expect(s[0].answer?.state).toBe("confirmed");
    const none = chatReducer(chatReducer([], { type: "start", id: "t2", question: "q" }), { type: "confirm", id: "t2", state: "sending" });
    expect(none[0].answer).toBeUndefined();
  });
});
