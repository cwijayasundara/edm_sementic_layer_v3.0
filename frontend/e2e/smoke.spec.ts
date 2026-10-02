import { expect, test } from "@playwright/test";
import { CHAT_BODY, KPIS, TOKEN, WIDGET, rows } from "./fixtures";

const AGENT = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000";

test.describe("mocked agent", () => {
  test.skip(!!process.env.PRISM_E2E_LIVE, "live mode runs the live test only");

  test("login → KPIs → ask → widget → provenance → pin, save, reopen", async ({ page }) => {
    const saved: any[] = [];
    await page.route(`${AGENT}/**`, async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json",
        body: JSON.stringify(body), headers: { "access-control-allow-origin": "http://localhost:3000" } });
      if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: {
        "access-control-allow-origin": "http://localhost:3000", "access-control-allow-headers": "Authorization, Content-Type",
        "access-control-allow-methods": "GET, POST, DELETE" } });
      if (url.pathname === "/dev/token") return json({ token: TOKEN });
      if (url.pathname === "/kpis") return json(KPIS);
      if (url.pathname === "/chat") return route.fulfill({ status: 200, contentType: "text/event-stream", body: CHAT_BODY,
        headers: { "access-control-allow-origin": "http://localhost:3000" } });
      if (url.pathname.startsWith("/results/")) {
        return json(rows(Number(url.searchParams.get("offset")), Number(url.searchParams.get("limit"))));
      }
      if (url.pathname === "/dashboards" && req.method() === "POST") { saved.push(req.postDataJSON()); return json({ id: "d1" }, 201); }
      if (url.pathname === "/dashboards") return json({ dashboards: saved.length
        ? [{ id: "d1", title: "Morning check", created_at: "2026-10-01T09:00:00Z", widget_count: 1 }] : [] });
      if (url.pathname === "/dashboards/d1/run") return json({ id: "d1", title: "Morning check", widgets: [
        { widget: { ...WIDGET, handle: "r_bbbbbbbbbbbb" }, status: "ok", handle_info: { columns: ["region", "value"],
          row_count: 60, source: "cashrecon", metric_id: "open_breaks", recipe: saved[0].items[0].recipe } }] });
      return json({ detail: "not found" }, 404);
    });

    await page.goto("/login");
    await page.getByRole("button", { name: "Sign in as Cash Ops Analyst - EMEA" }).click();
    await expect(page.getByText("CashRecon ✓")).toBeVisible();
    await expect(page.getByText("91.2%")).toBeVisible();

    await page.getByRole("textbox", { name: "Question" }).fill("How many open breaks are there by region?");
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.getByText("EMEA has the most open breaks.")).toBeVisible();
    await expect(page.getByText("Running metric open_breaks…")).toBeVisible();
    const card = page.getByRole("region", { name: "Canvas" }).locator("div").filter({ hasText: "Open breaks by region" }).first();
    await expect(card.locator("canvas").first()).toBeVisible();

    await page.getByRole("button", { name: "view query · source rows" }).first().click();
    await expect(page.getByText("open_breaks").first()).toBeVisible();
    await expect(page.getByText("Rows 1–50 of 60")).toBeVisible();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByText("Rows 51–60 of 60")).toBeVisible();
    await page.keyboard.press("Escape");

    await page.getByRole("button", { name: "Pin", exact: true }).first().click();
    await page.getByRole("button", { name: "Save pinned (1)" }).click();
    await page.getByRole("textbox", { name: "Dashboard title" }).fill("Morning check");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Dashboard saved.")).toBeVisible();
    expect(saved[0].items[0].recipe.args.metric_id).toBe("open_breaks");

    await page.getByRole("button", { name: "Dashboards" }).click();
    await page.getByRole("button", { name: "Open Morning check" }).click();
    await expect(page.getByText("Morning check").first()).toBeVisible();
  });
});

test.describe("live agent", () => {
  test.skip(!process.env.PRISM_E2E_LIVE, "set PRISM_E2E_LIVE=1 with scripts/start_backend.sh running");

  test("login and KPI strip against the real agent", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("button", { name: "Sign in as Head of Data Operations" }).click();
    await expect(page.getByRole("region", { name: "Key metrics" })).toBeVisible();
    await expect(page.getByText("Unavailable").or(page.getByText(/\d/)).first()).toBeVisible();
  });
});
