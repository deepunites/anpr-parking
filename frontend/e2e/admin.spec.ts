import { expect, test } from "@playwright/test";
import { loginUi, stack } from "./stack";

/** v2: администратор клиента редактирует площадку (места, подсети) и заводит сотрудника с временным паролем;
    сотрудник входит этим паролем, меняет его на свой и видит интерфейс своей роли. */
test("admin edits the lot and adds an employee who can log in", async ({ page }) => {
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);

  // своя площадка: общие A1/A2 не трогаем — от их подсетей и мест зависят остальные сценарии
  await page.getByRole("link", { name: "Площадки" }).click();
  const name = `E2E площадка ${Date.now() % 100000}`;
  await page.getByTestId("add-lot").click();
  await page.getByTestId("lot-name").fill(name);
  await page.getByTestId("lot-capacity").fill("60");
  await page.getByTestId("lot-submit").click();
  const row = page.getByTestId("lot-row").filter({ hasText: name });
  await expect(row).toContainText("60");
  await row.getByTestId("edit-lot").click();
  await page.getByTestId("lot-capacity").fill("150");
  await page.getByTestId("lot-cidrs").fill("10.100.5.0/24");
  await expect(page.getByTestId("lot-submit")).toBeDisabled(); // без причины сохранить нельзя
  await page.getByTestId("lot-reason").fill("расширение парковки");
  await page.getByTestId("lot-submit").click();
  await expect(row).toContainText("150");

  await page.getByRole("link", { name: "Сотрудники" }).click();
  await page.getByTestId("add-user").click();
  const email = `e2e.emp.${Date.now()}@alpha-parking.uz`;
  await page.getByTestId("user-name").fill("Нилуфар Саидова");
  await page.getByTestId("user-email").fill(email);
  await page.getByTestId("role-CASHIER").click();
  await page.getByTestId("user-submit").click();
  const password = await page.getByTestId("temp-password").innerText();
  expect(password).toHaveLength(16);
  await page.getByTestId("password-done").click();
  await expect(page.locator(`[data-testid='user-row'][data-email='${email}']`)).toContainText("кассир");

  await expect(page.locator(`[data-testid='user-row'][data-email='${email}']`).getByTestId("must-change")).toBeVisible();

  // v2 этап 4: временный пароль администратора — при первом входе сотрудник придумывает свой
  await page.getByTestId("logout").click();
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByTestId("login-submit").click();
  await page.waitForURL(/\/change-password$/);
  await page.getByTestId("pwd-current").fill(password);
  await page.getByTestId("pwd-new").fill("cashier-own-password");
  await page.getByTestId("pwd-repeat").fill("cashier-own-password");
  await page.getByTestId("pwd-submit").click();
  await page.waitForURL(/\/live$/);
  await expect(page.getByTestId("me-role")).toHaveText("кассир");
  await expect(page.getByRole("link", { name: "Касса" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Сотрудники" })).toHaveCount(0); // кассир сотрудниками не управляет
});
