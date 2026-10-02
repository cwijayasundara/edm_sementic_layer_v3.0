import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const seen = vi.hoisted(() => [] as unknown[]);
vi.mock("next/dynamic", () => ({ default: () => (p: { onSelect: (id: string) => void; highlight?: Set<string> }) => {
  seen.push(p.highlight);
  return <button type="button" data-testid="graph" onClick={() => p.onSelect("metric:open_breaks")}>graph</button>;
} }));
const lineage = vi.hoisted(() => vi.fn());
const trace = vi.hoisted(() => vi.fn());
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { lineage, trace } }));

import { ContextGraphDialog, GRAPH_EMPTY, GRAPH_UNAVAILABLE, NOT_GOVERNED, SHORTENED, TRACE_MISSING, TRACE_NONE } from "@/components/ContextGraphDialog";
import { EXPIRED_TEXT } from "@/components/WidgetCard";
import { ApiError } from "@/lib/api";
import { Trace } from "@/lib/schemas";

const G = { nodes: [{ id: "metric:open_breaks", kind: "Metric", label: "open_breaks", detail: "Open cash breaks" },
  { id: "source:cashrecon", kind: "Source", label: "cashrecon" }],
  edges: [{ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" }], truncated: false, governed: true };
const open = (handle = "r_aaaaaaaaaaaa") =>
  render(<ContextGraphDialog open onOpenChange={vi.fn()} handle={handle} title="Open breaks by region" />);

const T_MIN = Trace.parse({ run_id: "a".repeat(32), question: "q", answer: "A.", path: "metric", status: "ok",
  confirmed: false, created_at: 1, steps: [
    { seq: 0, parent: null, kind: "metric", label: "Ran metric open_breaks", note: null, considered: [], ms: 5,
      status: "ok", error_code: null, tool: "run_metric", args: null, handle: "r_aaaaaaaaaaaa", rows: 1, truncated: false,
      touched: [{ id: "metric:open_breaks", kind: "Metric", label: "open_breaks" }] },
    { seq: 1, parent: null, kind: "answer", label: "Answered", note: null, considered: [], ms: null, status: "ok",
      error_code: null, tool: null, args: null, handle: null, rows: null, truncated: null, touched: [] },
  ] });

beforeEach(() => { lineage.mockReset(); trace.mockReset(); seen.length = 0; });

describe("ContextGraphDialog", () => {
  it("loads the lineage of the handle and lists nodes by kind", async () => {
    lineage.mockResolvedValueOnce(G);
    open();
    expect(await screen.findByTestId("graph")).toBeInTheDocument();
    expect(lineage).toHaveBeenCalledWith("T", "r_aaaaaaaaaaaa");
    await userEvent.click(screen.getByText("List view"));
    expect(screen.getByText("cashrecon")).toBeInTheDocument();
  });

  it("keeps the dialog within the viewport and scrollable", async () => {
    lineage.mockResolvedValueOnce(G);
    open();
    await screen.findByTestId("graph");
    const content = document.querySelector('[data-slot="dialog-content"]')!;
    expect(content.className).toContain("max-h-[90vh]");
    expect(content.className).toContain("overflow-y-auto");
    expect(screen.getByText("Open breaks by region").closest("[data-slot='dialog-description']")).not.toBeNull();
  });

  it("shows a node's detail and neighbours when it is selected", async () => {
    lineage.mockResolvedValueOnce(G);
    open();
    await userEvent.click(await screen.findByTestId("graph"));
    expect(screen.getByText("Open cash breaks")).toBeInTheDocument();
    expect(screen.getByText(/PROVIDES/)).toBeInTheDocument();
  });

  it("notes free-form and shortened graphs", async () => {
    lineage.mockResolvedValueOnce({ ...G, governed: false, truncated: true });
    open();
    expect(await screen.findByText(NOT_GOVERNED)).toBeInTheDocument();
    expect(screen.getByText(SHORTENED)).toBeInTheDocument();
  });

  it("expired on 404, unavailable with retry otherwise, empty when there are no nodes", async () => {
    lineage.mockRejectedValueOnce(new ApiError(404));
    const { unmount } = open();
    expect(await screen.findByText(EXPIRED_TEXT)).toBeInTheDocument();
    unmount();
    lineage.mockRejectedValueOnce(new ApiError(502)).mockResolvedValueOnce({ ...G, nodes: [], edges: [] });
    open();
    expect(await screen.findByText(GRAPH_UNAVAILABLE)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(GRAPH_EMPTY)).toBeInTheDocument();
  });

  it("ignores a stale response after the handle changes", async () => {
    let resolveFirst: (v: unknown) => void = () => {};
    lineage.mockImplementationOnce(() => new Promise((r) => { resolveFirst = r; }))
      .mockResolvedValueOnce({ ...G, nodes: [{ id: "metric:b", kind: "Metric", label: "second" }], edges: [] });
    const { rerender } = open("r_aaaaaaaaaaaa");
    rerender(<ContextGraphDialog open onOpenChange={vi.fn()} handle="r_bbbbbbbbbbbb" title="B" />);
    await userEvent.click(await screen.findByText("List view"));
    resolveFirst(G);
    expect(await screen.findByText("second")).toBeInTheDocument();
    expect(screen.queryByText("cashrecon")).toBeNull();
  });

  it("opens on Reasoning, and graph tab is disabled without a handle", async () => {
    trace.mockResolvedValueOnce(T_MIN);
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" runId={"a".repeat(32)} initialTab="reasoning" />);
    expect(await screen.findByText("Answered")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Graph" })).toBeDisabled();
  });

  it("missing trace and no run id show their notices", async () => {
    lineage.mockResolvedValue(G);
    trace.mockRejectedValueOnce(new ApiError(404));
    const { unmount } = render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa"
      runId={"a".repeat(32)} initialTab="reasoning" />);
    expect(await screen.findByText(TRACE_MISSING)).toBeInTheDocument();
    unmount();
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa" initialTab="reasoning" />);
    expect(screen.getByText(TRACE_NONE)).toBeInTheDocument();
  });

  it("a chip switches to the graph tab with that node selected", async () => {
    lineage.mockResolvedValue(G);
    trace.mockResolvedValueOnce(T_MIN);
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa" runId={"a".repeat(32)}
      initialTab="reasoning" />);
    await userEvent.click(await screen.findByRole("button", { name: "open_breaks" }));
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("Open cash breaks")).toBeInTheDocument();
  });

  it("passes the graph the same highlight set across a node selection", async () => {
    lineage.mockResolvedValue(G);
    trace.mockResolvedValueOnce(T_MIN);
    render(<ContextGraphDialog open onOpenChange={vi.fn()} title="q" handle="r_aaaaaaaaaaaa" runId={"a".repeat(32)}
      initialTab="graph" />);
    const g = await screen.findByTestId("graph");
    await vi.waitFor(() => expect(seen.at(-1)).toBeInstanceOf(Set));
    const before = seen.at(-1);
    const renders = seen.length;
    await userEvent.click(g);
    expect(await screen.findByText("Open cash breaks")).toBeInTheDocument();
    expect(seen.length).toBeGreaterThan(renders);
    expect(seen.at(-1)).toBe(before);
  });
});
