import { expect, test } from "@playwright/test";
import { apiToken, cameraEvent, loginUi, masked, shown, stack, uniquePlate } from "./stack";

/** Gate: вход → live → въезд по событию камеры → коррекция номера → оплата наличными → выезд (сессия закрыта). */
test("operator flow: login → live → entry → plate correction → cash → exit", async ({ page, request }) => {
  const s = stack();
  const manager = s.users["manager"]!;
  await loginUi(page, manager);
  await page.getByTestId("lot-select").selectOption(s.lot_id);
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "open");

  // смена кассира открывается в UI (наличные без смены не сверяются)
  await page.getByRole("link", { name: "Касса" }).click();
  const token = await apiToken(request, s, manager);
  const current = await request.get(`${s.api}/api/v1/shifts/current`, { headers: { Authorization: `Bearer ${token}` } });
  if (current.status() === 404) {
    await page.getByTestId("opening-float").fill("50000");
    await page.getByTestId("open-shift").click();
  }
  await expect(page.getByTestId("shift-open")).toBeVisible();

  await page.getByRole("link", { name: "Живая парковка" }).click();
  await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "open");

  // въезд по событию камеры (2 часа назад — чтобы за стоянку начислилась сумма)
  const plate = uniquePlate();
  const entryAt = new Date(Date.now() - 2 * 3600_000).toISOString();
  const entry = await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate, event_time: entryAt });
  expect(entry.session_id).not.toBeNull();
  const feed = page.getByTestId("feed");
  await expect(feed.locator("li[data-type='ENTRY']").first()).toBeVisible();
  const row = page.getByTestId("list-active").locator(`[data-plate='${masked(plate)}']`);
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.getByTestId("session-plate")).toHaveText(shown(masked(plate)));

  // коррекция номера: исходное распознавание видно рядом, поле в фокусе, reason обязателен
  await page.getByTestId("correct-plate").click();
  await expect(page.getByTestId("original-plate")).toHaveText(shown(masked(plate)));
  await expect(page.getByTestId("new-plate")).toBeFocused();
  const corrected = plate.slice(0, 6) + "ZZ";
  await page.getByTestId("new-plate").fill(corrected);
  await expect(page.getByTestId("correct-submit")).toBeDisabled();
  await page.getByTestId("correct-reason").fill("камера прочитала последние символы неверно");
  await page.getByTestId("correct-submit").click();
  await expect(page.getByTestId("session-plate")).toHaveText(shown(masked(corrected)));

  // выезд → обязательство PENDING (сумма только с сервера) → наличные в UI
  const exit = await cameraEvent(request, s, s.camera_keys.exit, { camera_code: "EXIT_01", event_type: "EXIT", plate_number: corrected });
  expect(exit.status).toBe("PAYMENT_REQUIRED");
  await expect(page.getByTestId("amount-due")).toContainText("сум");
  await page.getByTestId("confirm-cash").click();
  await expect(page.locator("[data-testid='payment-row'][data-status='PAID']")).toBeVisible();

  // сага: платёж → шлагбаума у площадки нет → сессия закрыта; лента показывает выезд
  await expect(feed.locator("li[data-type='SESSION_CLOSED']").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("session-panel")).toContainText("Закрыта");
});
