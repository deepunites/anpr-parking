import { expect, test } from "@playwright/test";
import { apiToken, cameraEvent, loginUi, masked, stack, uniquePlate } from "./stack";

/** Gate: пользователь tenant B не видит данные tenant A ни на одном экране (список площадок, сессии, камеры,
 * шлагбаумы, пользователи, платежи) и не получает их напрямую через API. */
test("tenant B sees nothing of tenant A", async ({ page, request }) => {
  const s = stack();
  const plate = uniquePlate("01T");
  const entry = await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate });
  const sessionA = entry.session_id!;
  const tenantB = s.users["tenant_b_admin"]!;
  await loginUi(page, tenantB);
  await expect(page.getByTestId("lot-select").locator("option")).not.toHaveCount(0);
  const optionValues = await page.getByTestId("lot-select").locator("option").evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
  expect(optionValues.length).toBeGreaterThanOrEqual(1); // свои площадки есть
  expect(optionValues).not.toContain(s.lot_id); // площадки tenant A нет

  for (const path of ["/live", "/sessions", "/payments", "/cameras", "/barriers", "/users", "/access"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const html = await page.content();
    expect(html, path).not.toContain(plate);
    expect(html, path).not.toContain(masked(plate));
    expect(html, path).not.toContain("alpha-parking.uz");
    expect(html, path).not.toContain(s.lot_id);
  }
  const token = await apiToken(request, s, tenantB);
  const headers = { Authorization: `Bearer ${token}` };
  for (const url of [
    `/api/v1/sessions/${sessionA}`,
    `/api/v1/sessions/${sessionA}/payments`,
    `/api/v1/sessions/${sessionA}/images`,
    `/api/v1/lots/${s.lot_id}`,
    `/api/v1/lots/${s.lot_id}/sessions`,
    `/api/v1/lots/${s.lot_id}/occupancy`,
  ]) {
    const response = await request.get(`${s.api}${url}`, { headers });
    expect(response.status(), url).toBe(404);
  }
  const search = await request.get(`${s.api}/api/v1/sessions/search?plate=${plate}`, { headers });
  expect(search.status()).toBe(200);
  expect(await search.json()).toEqual([]);
  const cameras = await request.get(`${s.api}/api/v1/cameras`, { headers });
  expect(((await cameras.json()) as { lot_id: string }[]).every((c) => c.lot_id !== s.lot_id)).toBeTruthy();
});
