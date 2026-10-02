import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const dashboards = vi.hoisted(() => ({ list: vi.fn(), save: vi.fn(), remove: vi.fn(), run: vi.fn() }));
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { dashboards } }));

import { DashboardsMenu } from "@/components/DashboardsMenu";

const recipe = { tool: "run_metric" as const, args: { metric_id: "m", dimensions: [], filters: {}, limit: null } };
const widget = { id: "w1", type: "bar" as const, title: "T", handle: "r_aaaaaaaaaaaa", encoding: { x: "a", y: "b" } };

describe("DashboardsMenu", () => {
  it("saves pinned widgets under a title", async () => {
    dashboards.list.mockResolvedValue([]);
    dashboards.save.mockResolvedValue("d1");
    render(<DashboardsMenu pinned={[{ widget, recipe }]} onOpen={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Save pinned (1)" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Dashboard title" }), "Morning check");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(dashboards.save).toHaveBeenCalledWith("T", "Morning check", [{ widget, recipe }]));
  });

  it("disables save with nothing pinned, lists, opens and deletes dashboards", async () => {
    dashboards.list.mockResolvedValue([{ id: "d1", title: "Morning check", created_at: "2026-10-01T09:00:00Z", widget_count: 2 }]);
    dashboards.run.mockResolvedValue({ id: "d1", title: "Morning check", widgets: [] });
    dashboards.remove.mockResolvedValue(undefined);
    const onOpen = vi.fn();
    render(<DashboardsMenu pinned={[]} onOpen={onOpen} />);
    expect(screen.getByRole("button", { name: "Save pinned (0)" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Dashboards" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open Morning check" }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith({ id: "d1", title: "Morning check", widgets: [] }));
    await userEvent.click(screen.getByRole("button", { name: "Dashboards" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete Morning check" }));
    await waitFor(() => expect(dashboards.remove).toHaveBeenCalledWith("T", "d1"));
  });

  it("explains the 20-dashboard limit", async () => {
    dashboards.list.mockResolvedValue([]);
    const { ApiError } = await import("@/lib/api");
    dashboards.save.mockRejectedValue(new ApiError(409));
    render(<DashboardsMenu pinned={[{ widget, recipe }]} onOpen={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Save pinned (1)" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Dashboard title" }), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("You already have 20 dashboards. Delete one first.")).toBeInTheDocument();
  });

  it("disables save with a limit explanation when more than 8 widgets are pinned", () => {
    const nine = Array.from({ length: 9 }, (_, i) => ({ widget: { ...widget, id: `w${i}` }, recipe }));
    render(<DashboardsMenu pinned={nine} onOpen={vi.fn()} />);
    const btn = screen.getByRole("button", { name: "Save pinned (9)" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "A dashboard holds at most 8 widgets. Unpin some to save.");
  });

  it("shows no error message when the session has expired (Unauthorized)", async () => {
    const { Unauthorized } = await import("@/lib/api");
    const row = { id: "d1", title: "Morning check", created_at: "2026-10-01T09:00:00Z", widget_count: 2 };
    // list fails
    dashboards.list.mockRejectedValue(new Unauthorized());
    const a = render(<DashboardsMenu pinned={[{ widget, recipe }]} onOpen={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Dashboards" }));
    await waitFor(() => expect(dashboards.list).toHaveBeenCalled());
    expect(screen.queryByRole("status")).toBeNull();
    a.unmount();
    // save fails
    dashboards.list.mockResolvedValue([row]);
    dashboards.save.mockRejectedValue(new Unauthorized());
    const b = render(<DashboardsMenu pinned={[{ widget, recipe }]} onOpen={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Save pinned (1)" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Dashboard title" }), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(dashboards.save).toHaveBeenCalled());
    expect(screen.queryByText(/could not be saved/)).toBeNull();
    b.unmount();
    // run and remove fail
    dashboards.run.mockRejectedValue(new Unauthorized());
    dashboards.remove.mockRejectedValue(new Unauthorized());
    render(<DashboardsMenu pinned={[]} onOpen={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Dashboards" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open Morning check" }));
    await waitFor(() => expect(dashboards.run).toHaveBeenCalled());
    await userEvent.click(screen.getByRole("button", { name: "Dashboards" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete Morning check" }));
    await waitFor(() => expect(dashboards.remove).toHaveBeenCalled());
    expect(screen.queryByRole("status")).toBeNull();
  });
});
