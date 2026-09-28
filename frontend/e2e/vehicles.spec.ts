import { expect, test } from "@playwright/test";
import { loginUi, stack } from "./stack";

/** v2: машина с маркой. Номер вводится русскими буквами и сам становится латиницей; машина попадает в список с
    маркой; ручной въезд создаёт сессию, марка видна в очереди и правится из карточки; повторный въезд блокируется. */
test("add a car with make and register a manual entry", async ({ page }) => {
  const s = stack();
  await loginUi(page, s.users["manager"]!);
  await page.getByTestId("lot-select").selectOption(s.lot_id);
  const n = String(Date.now() % 1000).padStart(3, "0");

  await page.getByRole("link", { name: "Машины и доступ" }).click();
  await page.getByTestId("add-vehicle").click();
  await page.getByTestId("plate-input").fill(`01 а ${n} ва`);
  await expect(page.getByTestId("plate-input")).toHaveValue(`01 A ${n} BA`);
  await expect(page.getByTestId("plate-input-hint")).toContainText("частный номер");
  await page.getByTestId("quick-make-Chevrolet").click();
  await page.getByTestId("quick-model-Cobalt").click();
  await page.getByTestId("color-white").click();
  await page.getByTestId("kind-WHITELIST").click();
  await expect(page.getByTestId("vehicle-submit")).toBeDisabled(); // без комментария нельзя
  await page.getByTestId("vehicle-note").fill("сотрудник ТЦ");
  await page.getByTestId("vehicle-submit").click();
  await expect(page.locator(`[data-testid='rule-row'][data-plate='01A${n}BA']`)).toContainText("Chevrolet Cobalt · белый");

  await page.getByRole("link", { name: "Живая парковка" }).click();
  await page.getByTestId("manual-entry").click();
  await page.getByTestId("manual-plate").fill(`10 в ${n} се`);
  await page.getByTestId("quick-make-BYD").click();
  await page.getByTestId("color-black").click();
  await page.getByTestId("manual-ago-30").click();
  await page.getByTestId("manual-submit").click();
  await expect(page.getByTestId("session-vehicle")).toContainText("BYD · чёрный");
  await expect(page.getByTestId("session-panel")).toContainText("ручной въезд");

  await page.getByTestId("edit-vehicle").click();
  await page.getByTestId("vehicle-model").fill("Han");
  await page.getByTestId("vehicle-save").click();
  await expect(page.getByTestId("session-vehicle")).toContainText("BYD Han · чёрный");

  await page.getByTestId("manual-entry").click();
  await page.getByTestId("manual-plate").fill(`10B${n}CE`);
  await expect(page.getByTestId("manual-already-inside")).toBeVisible();
  await expect(page.getByTestId("manual-submit")).toBeDisabled();
});
