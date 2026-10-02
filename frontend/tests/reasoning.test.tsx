import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReasoningTimeline } from "@/components/ReasoningTimeline";
import { Trace } from "@/lib/schemas";

const T = Trace.parse({ run_id: "a".repeat(32), question: "q", answer: "Vendor A.", path: "metric", status: "ok",
  confirmed: true, created_at: 1, steps: [
    { seq: 0, parent: null, kind: "metric", label: "Ran metric price_conflicts by vendor_id", note: "Check conflicts",
      considered: [], ms: 1250, status: "ok", error_code: null, tool: "run_metric",
      args: "{\"metric_id\":\"price_conflicts\"}", handle: "r_aaaaaaaaaaaa", rows: 4, truncated: false,
      touched: [{ id: "metric:price_conflicts", kind: "Metric", label: "price_conflicts" }] },
    { seq: 1, parent: null, kind: "error", label: "Queried cashrecon", note: null, considered: [], ms: 10,
      status: "error", error_code: "not_permitted", tool: "query_source", args: null, handle: null, rows: null,
      truncated: null, touched: [] },
    { seq: 2, parent: null, kind: "answer", label: "Answered", note: null, considered: [], ms: null, status: "ok",
      error_code: null, tool: null, args: null, handle: null, rows: null, truncated: null, touched: [] },
  ] });

describe("ReasoningTimeline", () => {
  it("lists steps with rows, duration, note, args and badges", async () => {
    render(<ReasoningTimeline trace={T} onNode={vi.fn()} />);
    expect(screen.getByText("Ran metric price_conflicts by vendor_id")).toBeInTheDocument();
    expect(screen.getByText(/4 rows · 1\.3 s/)).toBeInTheDocument();
    expect(screen.getByText("Check conflicts")).toBeInTheDocument();
    expect(screen.getByText("not_permitted")).toBeInTheDocument();
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
    await userEvent.click(screen.getAllByText("Arguments")[0]);
    expect(screen.getByText(/"metric_id":"price_conflicts"/)).toBeInTheDocument();
  });

  it("a touched-node chip reports its id", async () => {
    const onNode = vi.fn();
    render(<ReasoningTimeline trace={T} onNode={onNode} />);
    await userEvent.click(screen.getByRole("button", { name: "price_conflicts" }));
    expect(onNode).toHaveBeenCalledWith("metric:price_conflicts");
  });
});
