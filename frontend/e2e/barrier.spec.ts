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
