import { expect, test } from "@playwright/test";
import { KPIS, TOKEN, WIDGET, chatBody, rows } from "./fixtures";

const AGENT = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000";

test.describe("mocked agent", () => {
  test.skip(!!process.env.PRISM_E2E_LIVE, "live mode runs the live test only");

  test("login → KPIs → ask → widget → provenance → pin, save, reopen", async ({ page }) => {
    const saved: any[] = [];
    const fetched: string[] = [];
    let asks = 0;
    let runCalls = 0;
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
      if (url.pathname === "/chat") return route.fulfill({ status: 200, contentType: "text/event-stream", body: chatBody(["r_aaaaaaaaaaaa", "r_dddddddddddd"][asks++]!),
        headers: { "access-control-allow-origin": "http://localhost:3000" } });
      if (url.pathname.startsWith("/results/")) {
        fetched.push(url.pathname.split("/").pop()!);
        return json(rows(Number(url.searchParams.get("offset")), Number(url.searchParams.get("limit"))));
      }
      if (url.pathname === "/dashboards" && req.method() === "POST") { saved.push(req.postDataJSON()); return json({ id: "d1" }, 201); }
      if (url.pathname === "/dashboards") return json({ dashboards: saved.length
        ? [{ id: "d1", title: "Morning check", created_at: "2026-10-01T09:00:00Z", widget_count: 2 }] : [] });
      if (url.pathname === "/dashboards/d1/run") {
        runCalls++;
        // both replayed widgets keep the per-turn id "w1", as real runs do
        return json({ id: "d1", title: "Morning check", widgets: ["r_bbbbbbbbbbbb", "r_cccccccccccc"].map((handle, i) => ({
          widget: { ...WIDGET, handle }, status: "ok", handle_info: { columns: ["region", "value"],
            row_count: 60, source: "cashrecon", metric_id: "open_breaks", recipe: saved[0].items[i].recipe } })) });
      }
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

    // a second question yields another widget that also has id "w1"
    await page.getByRole("textbox", { name: "Question" }).fill("And again, by region?");
    await page.getByRole("button", { name: "Ask" }).click();
    await expect.poll(() => fetched.includes("r_dddddddddddd")).toBe(true);
    await expect(page.getByRole("button", { name: "Pin", exact: true })).toHaveCount(1);
    await page.getByRole("button", { name: "Pin", exact: true }).click();
    await page.getByRole("button", { name: "Save pinned (2)" }).click();
    await page.getByRole("textbox", { name: "Dashboard title" }).fill("Morning check");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Dashboard saved.")).toBeVisible();
    expect(saved[0].items).toHaveLength(2);
    expect(saved[0].items[0].recipe.args.metric_id).toBe("open_breaks");

    await page.getByRole("button", { name: "Dashboards" }).click();
    await page.getByRole("button", { name: "Open Morning check" }).click();
    await expect.poll(() => runCalls).toBe(1);
    // only reopened cards carry the dashboard title as their origin line, inside the Canvas region
    const reopened = page.getByRole("region", { name: "Canvas" }).getByText("Morning check", { exact: true });
    await expect(reopened).toHaveCount(2);
    for (const i of [0, 1]) {
      await expect(reopened.nth(i).locator("xpath=ancestor::div[contains(@class,'flex-col')][1]").locator("canvas").first()).toBeVisible();
    }
    expect(fetched).toContain("r_bbbbbbbbbbbb");
    expect(fetched).toContain("r_cccccccccccc");
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
