import { expect, test } from "@playwright/test";

/** Gate: CSP без unsafe-inline/unsafe-eval и заголовки безопасности на собранном приложении (vite preview). */
test("security headers and no inline scripts in the built app", async ({ request, page }) => {
  const response = await request.get("/");
  const headers = response.headers();
  const csp = headers["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain("style-src 'self'");
  expect(csp).not.toContain("unsafe-inline");
  expect(csp).not.toContain("unsafe-eval");
  expect(headers["strict-transport-security"]).toContain("max-age=");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["referrer-policy"]).toBe("no-referrer");
  expect(headers["permissions-policy"]).toContain("camera=()");
  const html = await response.text();
  expect(html).not.toMatch(/<script(?![^>]*\bsrc=)/); // ни одного инлайн-скрипта
  expect(html).not.toMatch(/<style/);
  // приложение работает под этой CSP: страница логина рендерится, нарушений CSP нет
  const violations: string[] = [];
  page.on("console", (msg) => {
    if (msg.text().includes("Content Security Policy")) violations.push(msg.text());
  });
  await page.goto("/login");
  await expect(page.getByTestId("login-submit")).toBeVisible();
  expect(violations).toEqual([]);
});

/** Неверный пароль на входе — «Неверный логин или пароль», а не общее «Нужно войти заново»; e-mail несуществующий,
    чтобы неудача не легла в счётчик блокировки входа тестовых пользователей. */
test("wrong password on login is reported as wrong credentials", async ({ page }) => {
  await page.goto("/login");
  await page.locator("#email").fill("nobody@e2e.invalid");
  await page.locator("#password").fill("not-the-password");
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("error-box")).toContainText("Неверный логин или пароль");
  await expect(page.getByTestId("error-box")).not.toContainText("Нужно войти заново");
});
