import { expect, test } from "@playwright/test";
import { apiToken, cameraEvent, loginUi, masked, stack, uniquePlate } from "./stack";

/** Этап 6 v2, приёмка: вебхук session.closed доходит до тестового приёмника с верной подписью. */
test("webhook session.closed reaches the test inbox with a valid signature", async ({ page, request }) => {
  test.setTimeout(180_000);
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);
  await page.goto("/integrations?tab=webhooks");

  await page.getByTestId("create-inbox").click();
  await expect(page.getByTestId("inbox-url")).toContainText("/api/v1/hooks/inbox/");

  await page.getByTestId("add-webhook").click();
  await page.getByTestId("use-test-inbox").click();
  await expect(page.getByLabel("Адрес (URL)")).toHaveValue(/\/api\/v1\/hooks\/inbox\//);
  await expect(page.locator('[data-event="session.closed"]')).toBeChecked();
  await page.getByTestId("webhook-submit").click();
  await expect(page.getByTestId("secret-value")).toContainText("whsec_");
  await page.getByRole("button", { name: "Я сохранил" }).click();

  const card = page.getByTestId("webhook-card").filter({ hasText: "/api/v1/hooks/inbox/" }).first();
  await card.getByTestId("webhook-test").click();
  const inbox = page.getByTestId("test-inbox");
  await expect(inbox.locator('[data-testid="inbox-item"][data-event="webhook.test"]').first()).toContainText("подпись верна");

  // настоящая сессия: въезд камерой, закрытие оператором → consumer ставит доставку → диспетчер отправляет
  const plate = uniquePlate("01W");
  const entry = await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate });
  expect(entry.session_id).toBeTruthy();
  const token = await apiToken(request, s, s.users["tenant_admin"]!);
  const closed = await request.post(`${s.api}/api/v1/sessions/${entry.session_id}/close`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { reason: "e2e: проверка вебхука" },
  });
  expect(closed.ok()).toBeTruthy();

  const item = inbox.locator('[data-testid="inbox-item"][data-event="session.closed"]').filter({ hasText: entry.session_id! });
  await expect(item).toContainText("подпись верна", { timeout: 90_000 });
  await expect(item).not.toContainText(plate); // номера в вебхуки не попадают
  await expect(page.getByTestId("deliveries").locator('[data-status="DELIVERED"]').first()).toBeVisible();
});

/** Приёмка: тестовое событие Hikvision в песочнице; отклонённое событие камеры видно в журнале с причиной. */
test("sandbox checks a Hikvision event and a rejected camera event shows its reason", async ({ page, request }) => {
  test.setTimeout(120_000);
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);
  await page.getByTestId("lot-select").selectOption(s.lot_id); // площадка камер стенда
  await page.goto("/integrations?tab=sandbox");

  await expect(page.getByTestId("sandbox-payload")).toHaveValue(/EventNotificationAlert/);
  await page.getByTestId("sandbox-run").click();
  const result = page.getByTestId("sandbox-result");
  await expect(result.locator("[data-accepted]")).toHaveAttribute("data-accepted", "true");
  await expect(result).toContainText("01A777AA");

  const xml = await page.getByTestId("sandbox-payload").inputValue();
  await page.getByTestId("sandbox-payload").fill(xml.replace(/<confidenceLevel>\d+</, "<confidenceLevel>10<"));
  await page.getByTestId("sandbox-run").click();
  await expect(page.getByTestId("sandbox-reason")).toHaveText("LOW_CONFIDENCE");

  const plate = uniquePlate("01J");
  const rejected = await request.post(`${s.api}/api/v1/camera/events`, {
    headers: { "X-Camera-Key": s.camera_keys.entry },
    data: { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate, confidence: 5, direction: "IN", event_time: new Date().toISOString() },
  });
  expect(rejected.status()).toBe(422);

  await page.goto("/integrations?tab=journal");
  await page.getByRole("radio", { name: "отклонено", exact: true }).click();
  const row = page.getByTestId("journal-row").filter({ hasText: masked(plate) }).first();
  await expect(row).toContainText("LOW_CONFIDENCE", { timeout: 20_000 });
  await expect(row).toContainText("HTTP 422");
  await expect(page.getByTestId("journal")).not.toContainText(plate);
});

/** API-ключ: права — только выданные, после отзыва ключ не работает. */
test("api key is shown once, works within its scopes and stops after revoke", async ({ page, request }) => {
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);
  await page.goto("/integrations");
  await page.getByTestId("add-api-key").click();
  await page.getByLabel("Название").fill("E2E 1С");
  await page.locator('[data-scope="lots:read"]').check();
  await page.getByTestId("api-key-submit").click();
  const secret = (await page.getByTestId("secret-value").textContent())!.trim();
  expect(secret).toMatch(/^pk_/);
  await page.getByRole("button", { name: "Я сохранил" }).click();

  const auth = { Authorization: `Bearer ${secret}` };
  expect((await request.get(`${s.api}/api/v1/lots`, { headers: auth })).status()).toBe(200);
  expect((await request.get(`${s.api}/api/v1/users`, { headers: auth })).status()).toBe(403);

  const row = page.getByTestId("api-key-row").filter({ hasText: "E2E 1С" });
  await row.getByTestId("revoke-api-key").click();
  await page.getByTestId("reason-input").fill("e2e: ключ больше не нужен");
  await page.getByTestId("reason-confirm").click();
  await expect(row).toContainText("отозван");
  expect((await request.get(`${s.api}/api/v1/lots`, { headers: auth })).status()).toBe(401);
});
