import { expect, test } from "@playwright/test";
import { loginUi, stack } from "./stack";

/** Раздел «Тариф»: у новой площадки тарифа нет — администратор задаёт цены, видит пример расчёта тем же движком,
    сохраняет; затем меняет цену — появляется вторая версия, первая остаётся в истории. Своя площадка: тарифы A1/A2
    не трогаем — от них зависят суммы в остальных сценариях. Кассиру раздел не виден. */
test("admin sets and changes the lot tariff with a live preview", async ({ page }) => {
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);

  await page.getByRole("link", { name: "Площадки" }).click();
  const name = `E2E тариф ${Date.now() % 100000}`;
  await page.getByTestId("add-lot").click();
  await page.getByTestId("lot-name").fill(name);
  await page.getByTestId("lot-capacity").fill("40");
  await page.getByTestId("lot-submit").click();
  await expect(page.getByTestId("lot-row").filter({ hasText: name })).toBeVisible();
  await page.getByTestId("lot-select").selectOption({ label: name });

  await page.getByRole("link", { name: "Тариф" }).click();
  await expect(page.getByTestId("tariff-none")).toBeVisible();
  // стартовые значения формы: 15 мин бесплатно, 5 000 первый час, 3 000 далее, не больше 30 000 в сутки
  await expect(page.getByTestId("tf-preview-60")).toHaveText(/5\s000/);
  await page.getByTestId("tf-firstHour").fill("4000");
  await expect(page.getByTestId("tf-preview-60")).toHaveText(/4\s000/);
  await page.getByTestId("tf-firstHour").fill("-1");
  await expect(page.getByTestId("tf-publish")).toBeDisabled(); // неверная цена — не сохранить
  await page.getByTestId("tf-firstHour").fill("4000");
  await expect(page.getByTestId("tf-publish")).toBeDisabled(); // без основания — не сохранить
  await page.getByTestId("tf-reason").fill("e2e: цены открытия");
  await page.getByTestId("tf-publish").click();
  await expect(page.getByTestId("tariff-current")).toContainText("первый час 4 000");

  await page.getByTestId("tf-perHour").fill("3500");
  await page.getByTestId("tf-day-6").selectOption("free");
  await page.getByTestId("tf-reason").fill("e2e: выходной бесплатно");
  await page.getByTestId("tf-publish").click();
  await expect(page.getByTestId("tariff-current")).toContainText("Вс: бесплатно");
  await expect(page.getByTestId("tariff-version")).toHaveCount(2); // первая версия осталась в истории

  await page.getByTestId("logout").click();
  await loginUi(page, s.users["cashier"]!);
  await expect(page.getByRole("link", { name: "Тариф" })).toHaveCount(0);
});
