import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const chat = vi.hoisted(() => vi.fn());
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { chat } }));

import { AssistantPanel, planText } from "@/components/AssistantPanel";

const widget = { type: "widget" as const, widget: { id: "w1", type: "bar" as const, title: "T", handle: "r_aaaaaaaaaaaa",
  encoding: { x: "region", y: "value" } }, handle_info: { columns: ["region", "value"], row_count: 1, source: "cashrecon",
  metric_id: "open_breaks" } };

describe("AssistantPanel", () => {
  it("streams plan, widget and summary, and hands widgets to the canvas", async () => {
    chat.mockImplementation(async function* () {
      yield { type: "plan", tool: "run_metric", label: "metric open_breaks" };
      yield widget;
      yield { type: "summary", text: "EMEA has the most open breaks." };
      yield { type: "telemetry", run_id: "r", path: "metric", models: ["m"], input_tokens: 1200, output_tokens: 80,
        cache_read_input_tokens: 900, llm_turns: 2, tool_calls: 2, tool_latency_ms: 10, cost_usd: 0.0123 };
    });
    const onWidget = vi.fn();
    render(<AssistantPanel onWidget={onWidget} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "Open breaks by region?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("EMEA has the most open breaks.")).toBeInTheDocument();
    expect(screen.getByText("Running metric open_breaks…")).toBeInTheDocument();
    expect(screen.getByText(/metric · 1,280 tokens · 900 cached · ~\$0\.0123/)).toBeInTheDocument();
    expect(onWidget).toHaveBeenCalledWith(expect.stringMatching(/:w1$/), widget, "Open breaks by region?");
  });

  it("shows the agent's fixed error text and nothing else", async () => {
    chat.mockImplementation(async function* () {
      yield { type: "error", code: "gateway_unavailable", message: "The data service is unavailable." };
    });
    render(<AssistantPanel onWidget={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("The data service is unavailable.")).toBeInTheDocument();
  });

  it("stop aborts the stream and marks the turn stopped", async () => {
    chat.mockImplementation(async function* (_t: string, _q: string, signal: AbortSignal) {
      yield { type: "plan", tool: "query_source", label: "query cashrecon" };
      await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("x", "AbortError"))));
    });
    render(<AssistantPanel onWidget={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Querying cashrecon…");
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.getByText("Stopped")).toBeInTheDocument());
  });

  it("formats plan steps", () => {
    expect(planText({ type: "plan", tool: "combine", label: "combine" })).toBe("Combining results…");
  });
});
