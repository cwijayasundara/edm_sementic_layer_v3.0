import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KpiStripView } from "@/components/KpiStrip";

describe("KpiStripView", () => {
  it("formats ok tiles and shows unavailable tiles without a value", () => {
    render(<KpiStripView state={{ kind: "ok", tiles: [
      { label: "Auto-match rate", metric_id: "auto_match_rate", unit: "%", status: "ok", value: 91.24 },
      { label: "Open breaks", metric_id: "open_breaks", unit: "", status: "unavailable" },
    ] }} onRetry={vi.fn()} />);
    expect(screen.getByText("91.2%")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("shows one notice with retry when the data service is down", () => {
    const retry = vi.fn();
    render(<KpiStripView state={{ kind: "error" }} onRetry={retry} />);
    screen.getByRole("button", { name: "Retry" }).click();
    expect(retry).toHaveBeenCalled();
    expect(screen.getByText("KPIs are unavailable right now.")).toBeInTheDocument();
  });
});
