import { expect, test } from "@playwright/test";
import { cameraEvent, loginUi, stack, uniquePlate } from "./stack";

/** v2 этап 5: карта площадки. Быстрая схема на 2000 мест, машина через камеру въезда двигает счётчик зоны;
    в редакторе администратор добавляет зону и ряд мест, перетаскивает ряд, сохраняет новую версию. */
test("quick layout, live zone counter and editor", async ({ page, request }) => {
  test.setTimeout(180_000);
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);
  await page.getByTestId("lot-select").selectOption(s.lot_id);
  await page.getByRole("link", { name: "Карта парковки" }).click();
  await expect(page.getByTestId("map-empty").or(page.getByTestId("map-zone").first())).toBeVisible();
  if (await page.getByTestId("map-empty").isVisible()) await page.getByTestId("map-quick").click();

  const zone = page.getByTestId("map-zone").first();
  await expect(zone).toBeVisible();
  const count = zone.getByTestId("map-zone-count");
  const before = Number((await count.innerText()).split("/")[0]!.trim());
  await expect(page.getByTestId("layout-map").getByTestId("layout-device").first()).toBeVisible();

  await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: uniquePlate("01M") });
  await expect(count).toHaveText(new RegExp(`^${before + 1} /`), { timeout: 20_000 }); // WebSocket → карта

  await page.getByText("Редактор схемы").click();
  await page.getByTestId("add-zone").click();
  await page.getByTestId("prop-name").fill("Этаж 2");
  await page.getByTestId("add-row").click();
  await page.getByTestId("prop-count").fill("8");
  await expect(page.getByTestId("editor-dirty")).toBeVisible();

  const row = page.getByTestId("layout-editor-canvas").getByTestId("layout-row").last();
  const box = (await row.boundingBox())!;
  await page.mouse.move(box.x + 5, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 60, box.y + box.height / 2 + 40, { steps: 6 });
  await page.mouse.up();
  const moved = (await row.boundingBox())!;
  expect(moved.x).toBeGreaterThan(box.x + 20); // ряд переехал (привязка к сетке)

  await page.getByTestId("save-layout").click();
  await expect(page.getByText(/Схема сохранена — версия \d+/)).toBeVisible();
  await page.getByText("Живая карта").click();
  await expect(page.getByTestId("map-zone").filter({ hasText: "Этаж 2" }).getByTestId("map-zone-count")).toHaveText("0 / 8");
});
