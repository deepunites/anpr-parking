import { expect, test } from "@playwright/test";
import { apiToken, cameraEvent, loginUi, masked, shown, stack, uniquePlate } from "./stack";

/** Gate: оператор без разрешения barrier:open — кнопки нет И API отвечает 403 (видимость — не граница безопасности). */
test("viewer cannot open the barrier: no button and API 403", async ({ page, request }) => {
  const s = stack();
  const plate = uniquePlate("01V");
  const entry = await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate });
  const sessionId = entry.session_id!;
  await loginUi(page, s.users["viewer"]!);
  await page.getByTestId("lot-select").selectOption(s.lot_id);
  const row = page.getByTestId("list-active").locator(`[data-plate='${masked(plate)}']`);
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.getByTestId("session-plate")).toHaveText(shown(masked(plate)));
  await expect(page.getByTestId("open-exit")).toHaveCount(0);
  await expect(page.getByTestId("correct-plate")).toHaveCount(0);
  const token = await apiToken(request, s, s.users["viewer"]!);
  const response = await request.post(`${s.api}/api/v1/sessions/${sessionId}/open-exit`, { headers: { Authorization: `Bearer ${token}` }, data: { reason: "viewer tries" } });
  expect(response.status()).toBe(403);
  expect(((await response.json()) as { error: { code: string } }).error.code).toBe("FORBIDDEN");
  // а у менеджера кнопка есть и API принимает
  const managerToken = await apiToken(request, s, s.users["manager"]!);
  const ok = await request.post(`${s.api}/api/v1/sessions/${sessionId}/open-exit`, { headers: { Authorization: `Bearer ${managerToken}` }, data: { reason: "e2e manual open" } });
  expect(ok.status()).not.toBe(403); // право есть; 409 — сессия без открытого выезда, это не RBAC
});
