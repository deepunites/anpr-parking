import { expect, test } from "@playwright/test";
import { loginUi, stack } from "./stack";

/** v2: шлагбаум на сетевом Modbus-реле (как APOLLO пилотной площадки): «Проверить связь» читает реле без
    движения стрелы, «Открыть для проверки» ждёт итога команды, имитатор реле подтверждает поднятую стрелу. */
test("probe and test-open a Modbus relay barrier", async ({ page, request }) => {
  const s = stack() as ReturnType<typeof stack> & { relay_simulator?: string | null };
  test.skip(!s.relay_simulator, "у хоста нет не-loopback адреса — имитатор реле не запущен");
  const relay = s.relay_simulator!;
  const before = (await (await request.get(`${relay}state`)).json()) as { opens: number };

  await loginUi(page, s.users["tenant_admin"]!);
  await page.getByTestId("lot-select").selectOption("00000000-0000-4000-8000-00000000a101");
  await page.goto("/devices?tab=barriers");
  const row = page.getByTestId("barrier-row").filter({ hasText: "Шлагбаум въезд 1" });

  await row.getByTestId("barrier-probe").click();
  await expect(row.getByTestId("barrier-result")).toHaveAttribute("data-ok", "1");
  await expect(row.getByTestId("barrier-result")).toContainText("coils 0-7");
  expect(((await (await request.get(`${relay}state`)).json()) as { opens: number }).opens).toBe(before.opens); // стрела не двигалась

  await row.getByTestId("barrier-test").click();
  await expect(row.getByTestId("barrier-result")).toContainText("Стрела открыта", { timeout: 20_000 });
  const after = (await (await request.get(`${relay}state`)).json()) as { opens: number; boom: string };
  expect(after.opens).toBe(before.opens + 1);
  expect(["OPENING", "OPEN"]).toContain(after.boom);
});

/** Стрела на реле камеры Hikvision: сохранённый пароль камеры не показывается, поэтому адрес и пароль меняются
    формой «Адрес и пароль» — без пароля адрес не сменить (сервер иначе ответит CREDENTIALS_REQUIRED). Стрела без
    камеры: события общей площадки A1 её не открывают. */
test("Hikvision barrier: address and camera password are changed together", async ({ page }) => {
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);
  await page.getByTestId("lot-select").selectOption("00000000-0000-4000-8000-00000000a101");
  await page.goto("/devices?tab=barriers");
  const name = `Реле камеры ${Date.now() % 100000}`;
  await page.getByRole("button", { name: "Добавить стрелу" }).click();
  const create = page.getByTestId("barrier-form");
  await create.getByTestId("barrier-name").fill(name);
  await create.locator("#bar-camera").selectOption("");
  await create.getByTestId("barrier-endpoint").fill("http://10.10.1.64");
  await create.getByTestId("barrier-password").fill("e2e-camera-pass");
  await create.getByTestId("barrier-submit").click();
  const row = page.getByTestId("barrier-row").filter({ hasText: name });
  await expect(row).toContainText("http://10.10.1.64");

  await row.getByTestId("barrier-access").click();
  const form = page.getByTestId("barrier-access-form");
  await form.getByTestId("access-endpoint").fill("http://10.10.1.65");
  await expect(form.getByTestId("access-submit")).toBeDisabled(); // без пароля — нельзя
  await form.getByTestId("access-password").fill("e2e-camera-pass-2");
  await form.getByTestId("access-submit").click();
  await expect(form).toBeHidden();
  await expect(row).toContainText("http://10.10.1.65");
});
