import { test } from "@playwright/test";
import { chatBody } from "./fixtures";
const AGENT = "http://localhost:8000";
const OUT = "../.shots/";
const cors = { "access-control-allow-origin": "http://localhost:3000" };
const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
const tok = (sub: string, name: string, scopes: string[], rows: object) => `${b64({ alg: "HS256" })}.${b64({ sub, name, roles: [sub], scopes, rows, metrics_only: false, exp: Math.floor(Date.now() / 1000) + 3600 })}.sig`;
const PERSONA: Record<string, string> = {
  invest: tok("invest_ops_growth", "Investment Ops - Growth Funds", ["assetrecon", "feedhub", "refmaster.securities", "refmaster.legal_entities"], { fund_group: ["Growth"], source_type: ["custodian"], asset_class: ["*"] }),
  head: tok("head_data", "Head of Data Operations", ["refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub", "pii:read"], {}),
};
const LINEAGE = { truncated: false, governed: true, nodes: [
  { id: "metric:open_breaks", kind: "Metric", label: "open_breaks", source: "cashrecon", detail: "Open cash breaks" },
  { id: "source:cashrecon", kind: "Source", label: "cashrecon" },
  { id: "table:cashrecon.breaks", kind: "Table", label: "cashrecon.breaks", source: "cashrecon" },
  { id: "column:cashrecon.breaks.region", kind: "Column", label: "breaks.region", source: "cashrecon" },
  { id: "column:cashrecon.breaks.status", kind: "Column", label: "breaks.status", source: "cashrecon" },
  { id: "dimension:region", kind: "Dimension", label: "region", source: "cashrecon" },
  { id: "metric:stale_prices", kind: "Metric", label: "stale_prices", source: "marketmaster" },
  { id: "endpoint:marketmaster.prices", kind: "Endpoint", label: "GET /prices", source: "marketmaster" },
  { id: "field:marketmaster.prices.as_of", kind: "Field", label: "prices.as_of", source: "marketmaster" },
  { id: "term:break", kind: "BusinessTerm", label: "cash break" },
  { id: "question:q1", kind: "Question", label: "Which region has the most open breaks?" },
], edges: [
  { from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" },
  { from: "source:cashrecon", to: "table:cashrecon.breaks", type: "HAS_TABLE" },
  { from: "metric:open_breaks", to: "table:cashrecon.breaks", type: "COMPUTED_FROM" },
  { from: "table:cashrecon.breaks", to: "column:cashrecon.breaks.region", type: "HAS_COLUMN" },
  { from: "table:cashrecon.breaks", to: "column:cashrecon.breaks.status", type: "HAS_COLUMN" },
  { from: "metric:open_breaks", to: "dimension:region", type: "HAS_DIMENSION" },
  { from: "dimension:region", to: "column:cashrecon.breaks.region", type: "ON_COLUMN" },
  { from: "metric:stale_prices", to: "endpoint:marketmaster.prices", type: "COMPUTED_FROM" },
  { from: "endpoint:marketmaster.prices", to: "field:marketmaster.prices.as_of", type: "RETURNS" },
  { from: "metric:open_breaks", to: "metric:stale_prices", type: "JOINABLE_ON" },
  { from: "term:break", to: "metric:open_breaks", type: "DEFINES" },
  { from: "question:q1", to: "metric:open_breaks", type: "ANSWERED_BY" },
] };
async function mock(page: any, token: string) {
  let n = 0;
  await page.route(`${AGENT}/**`, async (route: any) => {
    const req = route.request(); const url = new URL(req.url());
    const json = (b: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b), headers: cors });
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: { ...cors, "access-control-allow-headers": "Authorization, Content-Type", "access-control-allow-methods": "GET, POST, DELETE" } });
    if (url.pathname === "/dev/token") return json({ token });
    if (url.pathname === "/kpis") return json({ tiles: [{ label: "Open position exceptions", metric_id: "a", unit: "", status: "ok", value: 37 }, { label: "NAV breaches > 5bps", metric_id: "b", unit: "", status: "ok", value: 4 }, { label: "Feeds on time", metric_id: "c", unit: "%", status: "ok", value: 88.4 }] });
    if (url.pathname === "/chat") return route.fulfill({ status: 200, contentType: "text/event-stream", body: chatBody(`r_aaaaaaaaaaa${n++}`), headers: cors });
    if (url.pathname.startsWith("/results/")) return json({ handle: "x", columns: ["region", "value"], offset: 0, row_count: 5, rows: ["EMEA", "APAC", "AMER", "LATAM", "UK"].map((r, i) => [r, [42, 31, 18, 9, 16][i]]) });
    if (url.pathname.startsWith("/lineage/")) return json(LINEAGE);
    if (url.pathname === "/dashboards") return json({ dashboards: [{ id: "d1", title: "Morning cash check", created_at: "2026-10-01T09:00:00Z", widget_count: 3 }] });
    return route.fulfill({ status: 404, body: "{}", headers: cors });
  });
}
async function login(page: any, name: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: `Sign in as ${name}` }).click();
  await page.getByRole("region", { name: "Key metrics" }).waitFor();
}
test("desktop states", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mock(page, PERSONA.invest);
  await login(page, "Investment Ops - Growth Funds");
  await page.getByRole("textbox", { name: "Question" }).fill("q");
  await page.getByRole("button", { name: "Ask" }).click();
  await page.getByText("EMEA has the most open breaks.").waitFor();
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}invest-1280.png` });
  await page.getByRole("button", { name: "Expand" }).click();
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}expand.png` });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Collapse assistant" }).click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}collapsed.png` });
  await page.getByRole("button", { name: "Open assistant" }).click();
});
test("context graph", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mock(page, PERSONA.head);
  await login(page, "Head of Data Operations");
  await page.getByRole("textbox", { name: "Question" }).fill("q");
  await page.getByRole("button", { name: "Ask" }).click();
  await page.getByText("EMEA has the most open breaks.").waitFor();
  await page.getByRole("button", { name: "Context graph" }).first().click();
  await page.getByRole("dialog").locator("canvas").first().waitFor();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}context-graph.png` });
  await page.getByRole("button", { name: /^CashRecon/ }).click();
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}context-graph-focus.png` });
});
test("context graph on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mock(page, PERSONA.head);
  await login(page, "Head of Data Operations");
  await page.getByRole("button", { name: "Assistant", exact: true }).click();
  await page.getByRole("textbox", { name: "Question" }).fill("q");
  await page.getByRole("button", { name: "Ask" }).click();
  await page.getByText("EMEA has the most open breaks.").waitFor();
  await page.getByRole("button", { name: "Close assistant" }).click();
  await page.getByRole("button", { name: "Context graph" }).first().click();
  await page.getByRole("dialog").locator("canvas").first().waitFor();
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${OUT}context-graph-phone.png` });
});
test("head header", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 500 });
  await mock(page, PERSONA.head);
  await login(page, "Head of Data Operations");
  await page.screenshot({ path: `${OUT}head-1280.png` });
});
test("mobile menu and save", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mock(page, PERSONA.invest);
  await login(page, "Investment Ops - Growth Funds");
  await page.getByRole("button", { name: "Dashboards" }).click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}mob-menu.png` });
  await page.getByRole("button", { name: "Dashboards" }).click();
  await page.getByRole("button", { name: "Assistant", exact: true }).click();
  await page.getByRole("textbox", { name: "Question" }).fill("q");
  await page.getByRole("button", { name: "Ask" }).click();
  await page.getByText("EMEA has the most open breaks.").waitFor();
  await page.getByRole("button", { name: "Close assistant" }).click();
  await page.getByRole("button", { name: "Pin", exact: true }).click();
  await page.getByRole("button", { name: /Save pinned/ }).click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}mob-save.png` });
});
