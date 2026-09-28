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
