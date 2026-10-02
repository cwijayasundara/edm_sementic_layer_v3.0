import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const chat = vi.hoisted(() => vi.fn());
const confirm = vi.hoisted(() => vi.fn());
const callImpl = vi.hoisted(() => ({ fn: null as null | ((fn: (t: string) => unknown) => unknown) }));
vi.mock("@/components/SessionProvider", () => ({
  useSession: () => ({ call: (fn: (t: string) => unknown) => (callImpl.fn ? callImpl.fn(fn) : fn("T")) }),
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { chat, confirm } }));

import { AssistantPanel, planText } from "@/components/AssistantPanel";
import { ApiError, Unauthorized } from "@/lib/api";

beforeEach(() => { callImpl.fn = null; chat.mockReset(); confirm.mockReset(); });

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
    const details = screen.getByText(/metric · 1,280 tokens · 900 cached · ~\$0\.0123/).closest("details")!;
    expect(details).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("Run details"));
    expect(details).toHaveAttribute("open");
    expect(onWidget).toHaveBeenCalledWith(expect.stringMatching(/:w1$/), widget, "Open breaks by region?");
  });

  it("renders the summary's markdown", async () => {
    chat.mockImplementation(async function* () { yield { type: "summary", text: "**Vendor A** drives most conflicts." }; });
    render(<AssistantPanel onWidget={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "Who?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect((await screen.findByText("Vendor A")).tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*/)).toBeNull();
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

  it("allows asking again right after Stop and ignores late widgets of the aborted stream", async () => {
    let release!: () => void;
    chat.mockImplementationOnce(async function* () {
      yield { type: "plan", tool: "query_source", label: "query cashrecon" };
      await new Promise<void>((r) => { release = r; });
      yield widget;
    });
    chat.mockImplementationOnce(async function* () {
      yield { type: "summary", text: "Second answer." };
    });
    const onWidget = vi.fn();
    render(<AssistantPanel onWidget={onWidget} />);
    const box = screen.getByRole("textbox", { name: "Question" });
    await userEvent.type(box, "first");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("Querying cashrecon…");
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    await userEvent.type(box, "second");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Second answer.")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(chat).toHaveBeenCalledTimes(2);
    release();
    await new Promise((r) => setTimeout(r, 20));
    expect(onWidget).not.toHaveBeenCalled();
  });

  it("shows the fixed fallback text when the stream throws a non-abort error", async () => {
    chat.mockImplementation(async function* () {
      throw new Error("boom internal detail");
    });
    render(<AssistantPanel onWidget={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("The assistant is not reachable. Please try again.")).toBeInTheDocument();
    expect(screen.queryByText(/boom/)).not.toBeInTheDocument();
  });

  it("renders no error text when the session rejects with Unauthorized", async () => {
    callImpl.fn = () => Promise.reject(new Unauthorized());
    render(<AssistantPanel onWidget={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("The assistant is not reachable. Please try again.")).not.toBeInTheDocument();
  });
});

const RID = "0b8f3c1e-2d4a-4c6b-9e7f-1a2b3c4d5e6f";
async function askWithAnswer(confirmable: boolean) {
  chat.mockImplementation(async function* () {
    yield { type: "summary", text: "Done." };
    yield { type: "answer", record_id: RID, confirmable };
  });
  render(<AssistantPanel onWidget={vi.fn()} />);
  await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "q");
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
  await screen.findByText("Done.");
}

describe("How I got this", () => {
  it("offers How I got this once telemetry arrives and reports the run id", async () => {
    chat.mockImplementation(async function* () {
      yield widget;
      yield { type: "summary", text: "Done." };
      yield { type: "telemetry", run_id: "a".repeat(32), path: "metric", models: ["m"], input_tokens: 1,
        output_tokens: 1, cache_read_input_tokens: 0, llm_turns: 1, tool_calls: 1, tool_latency_ms: 1, cost_usd: 0 };
    });
    const onRun = vi.fn(), onExplain = vi.fn();
    render(<AssistantPanel onWidget={vi.fn()} onRun={onRun} onExplain={onExplain} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "Q?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await userEvent.click(await screen.findByRole("button", { name: "How I got this" }));
    expect(onRun).toHaveBeenCalledWith(expect.any(String), "a".repeat(32));
    expect(onExplain).toHaveBeenCalledWith({ runId: "a".repeat(32), handle: "r_aaaaaaaaaaaa", title: "Q?" });
  });

  it("confirms with the run id when telemetry arrived", async () => {
    chat.mockImplementation(async function* () {
      yield { type: "summary", text: "Done." };
      yield { type: "answer", record_id: RID, confirmable: true };
      yield { type: "telemetry", run_id: "b".repeat(32), path: "metric", models: ["m"], input_tokens: 1,
        output_tokens: 1, cache_read_input_tokens: 0, llm_turns: 1, tool_calls: 1, tool_latency_ms: 1, cost_usd: 0 };
    });
    confirm.mockResolvedValue(undefined);
    render(<AssistantPanel onWidget={vi.fn()} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "q");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    await userEvent.click(await screen.findByRole("button", { name: "Confirm this answer" }));
    expect(confirm).toHaveBeenCalledWith("T", RID, "b".repeat(32));
  });
});

describe("confirming an answer", () => {
  it("confirms a confirmable answer once and shows Confirmed", async () => {
    let finish!: () => void;
    confirm.mockImplementation(() => new Promise<void>((r) => { finish = r; }));
    await askWithAnswer(true);
    const btn = screen.getByRole("button", { name: "Confirm this answer" });
    await userEvent.click(btn);
    expect(btn).toBeDisabled();
    await userEvent.click(btn);
    finish();
    expect(await screen.findByText("Confirmed")).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm).toHaveBeenCalledWith("T", RID, undefined);
    expect(screen.queryByRole("button", { name: "Confirm this answer" })).not.toBeInTheDocument();
  });

  it("shows fixed text when confirming fails and allows a retry", async () => {
    confirm.mockRejectedValueOnce(new ApiError(502)).mockResolvedValueOnce(undefined);
    await askWithAnswer(true);
    await userEvent.click(screen.getByRole("button", { name: "Confirm this answer" }));
    expect(await screen.findByText("Could not confirm this answer.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm this answer" }));
    expect(await screen.findByText("Confirmed")).toBeInTheDocument();
  });

  it("shows no confirm control for an answer that is not confirmable", async () => {
    await askWithAnswer(false);
    expect(screen.queryByRole("button", { name: "Confirm this answer" })).not.toBeInTheDocument();
  });
});
