import { expect, test } from "@playwright/test";
import { loginUi, stack } from "./stack";

/** v2 этап 4: администратор приглашает оператора по телефону, оператор открывает ссылку в своём браузере,
    придумывает пароль и сразу работает в своей роли; потом входит по телефону в местном формате. */
test("invite an operator by link, he joins and logs in by phone", async ({ page, browser }) => {
  test.setTimeout(180_000);
  const s = stack();
  const digits = `9${Date.now() % 10}${String(Date.now()).slice(-7)}`;
  const typed = `+998 ${digits.slice(0, 2)} ${digits.slice(2, 5)}-${digits.slice(5, 7)}-${digits.slice(7)}`;
  const name = `Оператор ${digits.slice(-4)}`;

  await loginUi(page, s.users["tenant_admin"]!);
  await page.getByRole("link", { name: "Сотрудники" }).click();
  await page.getByTestId("invite-user").click();
  await page.getByTestId("invite-name").fill(name);
  await page.getByTestId("invite-contact").fill(typed);
  await page.getByTestId("role-OPERATOR").click();
  await page.getByTestId("invite-submit").click();
  const link = await page.getByTestId("invite-link").innerText();
  expect(link).toMatch(/\/join\/[A-Za-z0-9_-]{40,}$/);
  await page.getByTestId("invite-done").click();
  await expect(page.getByTestId("invite-row").filter({ hasText: name })).toHaveAttribute("data-status", "PENDING");

  // оператор — в своём браузере, без входа
  const guest = await browser.newContext({ locale: "ru-RU" });
  const op = await guest.newPage();
  await op.goto(new URL(link).pathname);
  await expect(op.getByTestId("join-page")).toContainText("Alpha Parking");
  await expect(op.getByTestId("join-login")).toHaveText(typed);
  await op.getByTestId("join-password").fill("operator-own-password");
  await op.getByTestId("join-password2").fill("operator-own-passwrd");
  await expect(op.getByTestId("join-submit")).toBeDisabled(); // пароли не совпадают
  await op.getByTestId("join-password2").fill("operator-own-password");
  await op.getByTestId("join-submit").click();
  await op.waitForURL(/\/live$/);
  await expect(op.getByTestId("me-role")).toHaveText("оператор");
  await expect(op.getByRole("link", { name: "Сотрудники" })).toHaveCount(0);

  // ссылка одноразовая
  await op.getByTestId("logout").click();
  await op.goto(new URL(link).pathname);
  await expect(op.getByTestId("join-invalid")).toContainText("Приглашение уже принято");

  // вход по телефону в местном формате
  await op.goto("/login");
  await op.getByLabel("E-mail").fill(`${digits.slice(0, 2)} ${digits.slice(2)}`);
  await op.getByLabel("Пароль").fill("operator-own-password");
  await op.getByTestId("login-submit").click();
  await op.waitForURL(/\/live$/);
  await guest.close();

  // у администратора: приглашение принято (ушло из списка), сотрудник — с телефоном
  await page.reload();
  await expect(page.getByTestId("invite-row").filter({ hasText: name })).toHaveCount(0);
  await expect(page.getByTestId("user-row").filter({ hasText: name })).toContainText(typed);
});
