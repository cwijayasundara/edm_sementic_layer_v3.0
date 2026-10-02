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
