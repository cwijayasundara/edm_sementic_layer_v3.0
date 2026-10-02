import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const results = vi.hoisted(() => vi.fn());
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { results } }));

import { ProvenanceDrawer } from "@/components/ProvenanceDrawer";
import type { CanvasItem } from "@/lib/canvas";

const item: CanvasItem = {
  key: "t1:w1", origin: "q", pinned: false, status: "ok",
  widget: { id: "w1", type: "table", title: "Breaks", handle: "r_aaaaaaaaaaaa", encoding: {} },
  info: { columns: ["region", "value"], row_count: 120, source: "cashrecon", metric_id: null,
    recipe: { tool: "query_source", args: { source: "cashrecon", request: { sql: "SELECT region, count(*) AS value FROM breaks GROUP BY 1" } } } },
};

describe("ProvenanceDrawer", () => {
  it("shows the executed SQL, source and row count, and pages rows 50 at a time", async () => {
    results.mockImplementation(async (_t: string, _h: string, offset: number) => ({ handle: "r_aaaaaaaaaaaa",
      columns: ["region", "value"], offset, row_count: 120, rows: [[`row-${offset}`, 1]] }));
    render(<ProvenanceDrawer item={item} onClose={vi.fn()} />);
    expect(await screen.findByText(/SELECT region, count\(\*\)/)).toBeInTheDocument();
    expect(screen.getByText("cashrecon")).toBeInTheDocument();
    expect(await screen.findByText("row-0")).toBeInTheDocument();
    expect(screen.getByText("Rows 1–50 of 120")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("row-50")).toBeInTheDocument();
    expect(results).toHaveBeenLastCalledWith("T", "r_aaaaaaaaaaaa", 50, 50);
  });

  it("says so when the recipe is missing", async () => {
    results.mockResolvedValue({ handle: "r_aaaaaaaaaaaa", columns: [], offset: 0, row_count: 0, rows: [] });
    render(<ProvenanceDrawer item={{ ...item, info: { ...item.info!, recipe: null } }} onClose={vi.fn()} />);
    expect(await screen.findByText("Query details are not available for this result.")).toBeInTheDocument();
  });
});

describe("ProvenanceDrawer item switch", () => {
  it("drops the previous item's rows and fetches the new item from offset 0", async () => {
    results.mockReset();
    results.mockImplementation(async (_t: string, h: string, offset: number) => ({ handle: h,
      columns: ["region", "value"], offset, row_count: 120, rows: [[`${h}-${offset}`, 1]] }));
    const b: CanvasItem = { ...item, key: "t2:w2", widget: { ...item.widget, id: "w2", title: "Other", handle: "r_bbbbbbbbbbbb" } };
    const { rerender } = render(<ProvenanceDrawer item={item} onClose={vi.fn()} />);
    expect(await screen.findByText("r_aaaaaaaaaaaa-0")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("r_aaaaaaaaaaaa-50")).toBeInTheDocument();
    rerender(<ProvenanceDrawer item={b} onClose={vi.fn()} />);
    expect(screen.queryByText("r_aaaaaaaaaaaa-50")).not.toBeInTheDocument();
    expect(await screen.findByText("r_bbbbbbbbbbbb-0")).toBeInTheDocument();
    expect(results).not.toHaveBeenCalledWith("T", "r_bbbbbbbbbbbb", 50, 50);
    expect(results).toHaveBeenLastCalledWith("T", "r_bbbbbbbbbbbb", 0, 50);
  });
});
