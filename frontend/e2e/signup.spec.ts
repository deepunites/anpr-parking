import { expect, test } from "@playwright/test";
import { stack } from "./stack";

/** v2 этап 3 «Продажа»: клиент сам регистрируется (тариф START, код подтверждения), проходит мастер
    (площадка → цена → камеры с ключами → сотрудники), упирается в лимит тарифа с понятной ошибкой;
    владелец платформы видит нового клиента и отмечает оплату наличными. */
test("self-signup, setup wizard, plan limit and cash payment by platform owner", async ({ page }) => {
  test.setTimeout(300_000); // длинный сквозной сценарий (регистрация, мастер, лимит, второй вход) на слабой машине
  const s = stack();
  const n = Date.now().toString(36);
  const company = `E2E Парковка ${n}`;

  await page.goto("/login");
  await page.getByTestId("signup-link").click();
  await expect(page).toHaveURL(/\/signup$/);
  await page.getByTestId("plan-START").click();
  await page.getByLabel("Компания или парковка").fill(company);
  await page.getByLabel("Ваше имя").fill("Азиз Каримов");
  await page.getByLabel("Телефон").fill("+998 90 123-45-67");
  await page.getByLabel("E-mail для входа").fill(`owner.${n}@e2e-client.uz`);
  await page.getByLabel("Пароль").fill("e2e-signup-password");
  await expect(page.getByTestId("su-submit")).toBeDisabled(); // без согласия с условиями нельзя
  await page.getByTestId("su-terms").check();
  await page.getByTestId("su-submit").click();

  await expect(page.getByTestId("su-dev-code")).toBeVisible();
  await page.getByTestId("su-code").fill("000000");
  await page.getByTestId("su-confirm").click();
  await expect(page.getByTestId("error-box")).toContainText("Неверный код");
  await page.getByRole("button", { name: "Подставить" }).click();
  await page.getByTestId("su-confirm").click();
  await page.waitForURL(/\/welcome$/);

  await expect(page.getByTestId("tenant-name")).toHaveText(company);
  await expect(page.getByTestId("sub-banner")).toContainText("осталось 14 дн.");

  // мастер: площадка → цена → камеры → сотрудники → готово
  await page.getByTestId("wz-lot-name").fill("Главная");
  await page.getByTestId("wz-lot-capacity").fill("60");
  await page.getByTestId("wz-lot-save").click();
  await page.getByTestId("wz-tariff-save").click();
  await page.getByTestId("wz-cameras-add").click();
  const keys = page.getByTestId("wizard-keys");
  await expect(keys.locator("code")).toHaveCount(2);
  await page.getByTestId("wz-keys-next").click();
  await page.getByTestId("wz-map-quick").click(); // v2 этап 5: быстрая схема — 60 мест рядами, камеры у ворот
  await expect(page.getByTestId("wizard-map-preview").getByTestId("layout-device")).toHaveCount(2);
  await page.getByTestId("wz-map-next").click();
  await page.getByTestId("wz-team-next").click();
  await expect(page.getByTestId("wizard-done")).toBeVisible();

  // подписка: использование упёрлось в START (1 площадка, 2 камеры)
  await page.getByRole("link", { name: "Подписка" }).click();
  await expect(page.getByTestId("usage-lots")).toContainText("1 / 1");
  await expect(page.getByTestId("usage-cameras")).toContainText("2 / 2");

  // вторая площадка — понятная ошибка лимита и путь к смене тарифа
  await page.getByRole("link", { name: "Площадки" }).click();
  await page.getByTestId("add-lot").click();
  await page.getByTestId("lot-name").fill("Вторая");
  await page.getByTestId("lot-capacity").fill("20");
  await page.getByTestId("lot-submit").click();
  await expect(page.getByTestId("limit-line")).toContainText("Площадки: нужно 2, в тарифе START — 1");
  await expect(page.getByTestId("upgrade-link")).toBeVisible();

  // владелец платформы: клиент в списке, оплата наличными за 3 месяца
  await page.keyboard.press("Escape"); // закрыть панель формы площадки
  await page.getByTestId("logout").click();
  await page.waitForURL(/\/login$/);
  const owner = s.users["platform_owner"]!;
  await page.getByLabel("E-mail").fill(owner.email);
  await page.getByLabel("Пароль").fill(owner.password);
  await page.getByTestId("login-submit").click();
  await page.waitForURL(/\/platform$/);
  await page.getByLabel("Поиск по названию или коду").fill(company);
  const row = page.locator("tr", { hasText: company });
  await expect(row).toContainText("Пробный период");
  await expect(row).toContainText("1 · 60 · 2");
  await row.getByRole("button", { name: "Управлять" }).click();
  await page.getByTestId("months-3").click();
  await expect(page.getByTestId("paid-until-preview")).toBeVisible();
  await page.getByTestId("pt-reason").fill("Оплата наличными, квитанция 17");
  await page.getByTestId("pt-apply").click();
  await expect(row).toContainText("Активна");
});
