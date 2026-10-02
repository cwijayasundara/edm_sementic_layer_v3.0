import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/dynamic", () => ({ default: () => (p: { onSelect: (id: string) => void }) =>
  <button type="button" data-testid="graph" onClick={() => p.onSelect("metric:open_breaks")}>graph</button> }));
const lineage = vi.hoisted(() => vi.fn());
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { lineage } }));

import { ContextGraphDialog, GRAPH_EMPTY, GRAPH_UNAVAILABLE, NOT_GOVERNED, SHORTENED } from "@/components/ContextGraphDialog";
import { EXPIRED_TEXT } from "@/components/WidgetCard";
import { ApiError } from "@/lib/api";

const G = { nodes: [{ id: "metric:open_breaks", kind: "Metric", label: "open_breaks", detail: "Open cash breaks" },
  { id: "source:cashrecon", kind: "Source", label: "cashrecon" }],
  edges: [{ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" }], truncated: false, governed: true };
const open = (handle = "r_aaaaaaaaaaaa") =>
  render(<ContextGraphDialog open onOpenChange={vi.fn()} handle={handle} title="Open breaks by region" />);

beforeEach(() => lineage.mockReset());

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
});
