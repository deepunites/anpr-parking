import { expect, test, type APIRequestContext } from "@playwright/test";
import { apiToken, loginUi, stack, type Stack } from "./stack";

const BARRIER_A1_IN = "00000000-0000-4000-8000-00000000e101";
const LOT_A1 = "00000000-0000-4000-8000-00000000a101";

async function cashierShiftWithShortage(request: APIRequestContext, s: Stack): Promise<void> {
  const token = await apiToken(request, s, s.users["cashier"]!);
  const auth = { Authorization: `Bearer ${token}` };
  const current = await request.get(`${s.api}/api/v1/shifts/current`, { headers: auth });
  if (current.ok()) {
    const open = (await current.json()) as { id: string; opening_float: number };
    await request.post(`${s.api}/api/v1/shifts/${open.id}/close`, { headers: auth, data: { counted_cash: open.opening_float } });
  }
  const opened = await request.post(`${s.api}/api/v1/shifts`, { headers: auth, data: { lot_id: LOT_A1, opening_float: 5_000_000 } });
  expect(opened.ok()).toBeTruthy();
  const shift = (await opened.json()) as { id: string };
  // пересчитали на 20 000 сум меньше разменного фонда — недостача
  const closed = await request.post(`${s.api}/api/v1/shifts/${shift.id}/close`, { headers: auth, data: { counted_cash: 3_000_000 } });
  expect(closed.ok()).toBeTruthy();
}

/** Этап 7 v2, приёмка: сигналы «расхождение кассы» и «много ручных открытий» появляются от реальных действий. */
test("owner dashboard shows week figures and signals caused by real actions", async ({ page, request }) => {
  test.setTimeout(150_000);
  const s = stack();
  await cashierShiftWithShortage(request, s);
  const operator = await apiToken(request, s, s.users["operator"]!);
  for (let i = 0; i < 6; i++) {
    const r = await request.post(`${s.api}/api/v1/barriers/${BARRIER_A1_IN}/open`, {
      headers: { Authorization: `Bearer ${operator}` },
      data: { reason: "e2e: водитель без номера" },
    });
    expect(r.status()).toBe(202);
  }

  await loginUi(page, s.users["owner"]!);
  await page.goto("/dashboard");
  const board = page.getByTestId("owner-dashboard");
  await expect(board.getByTestId("kpi-revenue")).toBeVisible();
  await expect(board.getByTestId("chart-revenue")).toBeVisible();
  await expect(board.locator('[data-signal="CASH_DISCREPANCY"]')).toContainText(/недостача 20\s000/);
  await expect(board.locator('[data-signal="MANY_MANUAL_OPENS"]')).toBeVisible();
  const operatorRow = board.getByTestId("manual-row").filter({ hasText: "e2e.operator" });
  await expect(operatorRow).toContainText(/\b([6-9]|\d{2,})\b/);
  await expect(board.getByTestId("shift-row").first()).toContainText(/20\s000/);

  // таблица вместо графика — для тех, кому цвет не помогает (и для печати)
  await board.getByTestId("revenue-table-toggle").click();
  await expect(board.getByTestId("revenue-table")).toBeVisible();
  await page.getByRole("radio", { name: "30 дней" }).click();
  await expect(board.getByTestId("revenue-table").locator("tbody tr")).toHaveCount(30);
});

/** Владелец платформы: клиенты, MRR, внимание и здоровье (без Prometheus — локальные признаки). */
test("platform owner sees clients, MRR, attention and health", async ({ page }) => {
  const s = stack();
  await page.goto("/login");
  await page.getByLabel("E-mail").fill(s.users["platform_owner"]!.email);
  await page.getByLabel("Пароль").fill(s.users["platform_owner"]!.password);
  await page.getByTestId("login-submit").click();
  await page.waitForURL(/\/platform$/);
  const summary = page.getByTestId("platform-summary");
  await expect(summary.getByTestId("kpi-clients")).toBeVisible();
  await expect(summary.getByTestId("kpi-mrr")).toContainText("сум");
  await expect(page.getByTestId("platform-health")).toContainText("локально");
});
