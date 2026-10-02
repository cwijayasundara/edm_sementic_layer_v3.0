import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// WidgetCard loads Chart through next/dynamic; stub the loader so the chart renders synchronously
vi.mock("next/dynamic", () => ({ default: () => () => <div data-testid="chart" /> }));
const results = vi.hoisted(() => vi.fn());
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { results } }));

import { EXPIRED_TEXT, STATUS_TEXT, WidgetCard } from "@/components/WidgetCard";
import { ApiError } from "@/lib/api";
import type { CanvasItem } from "@/lib/canvas";

const base: CanvasItem = {
  key: "t1:w1", origin: "How many open breaks?", pinned: false, status: "ok",
  widget: { id: "w1", type: "bar", title: "Open breaks by region", handle: "r_aaaaaaaaaaaa", encoding: { x: "region", y: "value" } },
  info: { columns: ["region", "value"], row_count: 250, source: "cashrecon", metric_id: "open_breaks",
    recipe: { tool: "run_metric", args: { metric_id: "open_breaks", dimensions: ["region"], filters: {}, limit: null } } },
};
const props = { onRemove: vi.fn(), onTogglePin: vi.fn(), onProvenance: vi.fn() };

describe("WidgetCard", () => {
  it("renders a chart and says when only the first 200 rows are shown", async () => {
    results.mockResolvedValueOnce({ handle: "r_aaaaaaaaaaaa", columns: ["region", "value"], offset: 0, row_count: 250,
      rows: [["EMEA", 3]] });
    render(<WidgetCard item={base} {...props} />);
    expect(await screen.findByTestId("chart")).toBeInTheDocument();
    expect(screen.getByText("Showing the first 200 of 250 rows")).toBeInTheDocument();
    expect(results).toHaveBeenCalledWith("T", "r_aaaaaaaaaaaa", 0, 200);
  });

  it("shows the expired message on a 404", async () => {
    results.mockRejectedValueOnce(new ApiError(404));
    render(<WidgetCard item={base} {...props} />);
    expect(await screen.findByText(EXPIRED_TEXT)).toBeInTheDocument();
  });

  it("falls back to a table when the chart cannot be built", async () => {
    results.mockResolvedValueOnce({ handle: "r_aaaaaaaaaaaa", columns: ["desk", "value"], offset: 0, row_count: 1,
      rows: [["D1", 3]] });
    render(<WidgetCard item={base} {...props} />);
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("renders a status placeholder without fetching for a not-permitted dashboard widget", async () => {
    results.mockClear();
    render(<WidgetCard item={{ ...base, status: "not_permitted", info: null }} {...props} />);
    expect(screen.getByText(STATUS_TEXT.not_permitted)).toBeInTheDocument();
    await waitFor(() => expect(results).not.toHaveBeenCalled());
  });

  it("disables pin for a widget without a recipe and explains why", async () => {
    results.mockResolvedValueOnce({ handle: "r_aaaaaaaaaaaa", columns: ["region", "value"], offset: 0, row_count: 1,
      rows: [["EMEA", 3]] });
    render(<WidgetCard item={{ ...base, info: { ...base.info!, recipe: null } }} {...props} />);
    expect(screen.getByRole("button", { name: /pin/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /pin/i })).toHaveAttribute("title",
      "This result cannot be saved: its query details are not available.");
  });
});
