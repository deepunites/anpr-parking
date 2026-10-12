import { expect, test } from "@playwright/test";
import { apiToken, cameraEvent, loginUi, stack, uniquePlate } from "./stack";

/** Номера целиком — решение владельца: «Площадки» → «Номера в панели: Полностью» (с причиной). Тогда кассир видит
    номер въехавшей машины без звёздочек — в списке и в живой ленте. Политика площадки после теста возвращается:
    остальные сценарии ждут маску. */
test("lot shows full plates after the owner turns it on", async ({ page, request }) => {
  const s = stack();
  const admin = s.users["tenant_admin"]!;
  const auth = { Authorization: `Bearer ${await apiToken(request, s, admin)}` };
  const lots = (await (await request.get(`${s.api}/api/v1/lots`, { headers: auth })).json()) as { id: string; code: string; policies: Record<string, unknown> }[];
  const lot = lots.find((l) => l.id === s.lot_id)!;
  try {
    await loginUi(page, admin);
    await page.getByTestId("lot-select").selectOption(s.lot_id);
    await page.goto("/lots");
    await page.getByTestId("lot-row").and(page.locator(`[data-code="${lot.code}"]`)).getByTestId("edit-lot").click();
    const plates = page.getByTestId("lot-plates");
    await expect(plates.getByRole("radio", { name: "Маской (01A***AA)" })).toHaveAttribute("aria-checked", "true");
    await plates.getByRole("radio", { name: "Полностью" }).click();
    await page.getByTestId("lot-reason").fill("кассиры сверяют номер с машиной");
    await page.getByTestId("lot-submit").click();
    await expect(page.getByTestId("lot-form")).toBeHidden();

    await page.getByRole("link", { name: "Живая парковка" }).click();
    await expect(page.getByTestId("connection-indicator")).toHaveAttribute("data-status", "open");
    const plate = uniquePlate();
    await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate });
    await expect(page.getByTestId("list-active").locator(`[data-plate='${plate}']`)).toBeVisible();
    await expect(page.getByTestId("feed").locator("li[data-type='ENTRY']").first()).not.toContainText("*");
  } finally {
    await request.patch(`${s.api}/api/v1/lots/${s.lot_id}`, { headers: auth, data: { policies: lot.policies, reason: "вернуть политику площадки после теста" } });
  }
});
