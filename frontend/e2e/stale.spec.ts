import { expect, test } from "@playwright/test";
import { apiToken, cameraEvent, loginUi, stack, uniquePlate } from "./stack";

/** «Закрыть старые сессии»: машины уезжали незамеченными (камеры выезда не было) — незакрытые сессии старше N часов
    становятся «Осиротевшими». Сначала окно показывает, сколько попадёт; закрыть без причины нельзя; свежая сессия
    (машина стоит сейчас) не трогается. Порог 12 ч: сессии других сценариев — свежие. */
test("stale sessions are closed after preview and with a reason", async ({ page, request }) => {
  const s = stack();
  const manager = s.users["manager"]!;
  const auth = { Authorization: `Bearer ${await apiToken(request, s, manager)}` };
  const old = await cameraEvent(request, s, s.camera_keys.entry, {
    camera_code: "ENTRY_01",
    event_type: "ENTRY",
    plate_number: uniquePlate(),
    event_time: new Date(Date.now() - 20 * 3600_000).toISOString(),
  });
  const fresh = await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: uniquePlate() });
  expect(old.session_id).not.toBeNull();

  await loginUi(page, manager);
  await page.getByTestId("lot-select").selectOption(s.lot_id);
  await page.getByRole("link", { name: "Сессии" }).click();
  await page.getByTestId("close-stale").click();
  const form = page.getByTestId("close-stale-form");
  await form.getByTestId("stale-hours").fill("12");
  await expect(form.getByTestId("stale-found")).not.toHaveAttribute("data-count", "");
  await expect(form.getByTestId("stale-found")).not.toHaveAttribute("data-count", "0");
  await expect(form.getByTestId("stale-submit")).toBeDisabled(); // без причины — нельзя
  await form.getByTestId("stale-reason").fill("камеры выезда не было — машины уезжали незамеченными");
  await form.getByTestId("stale-submit").click();
  await expect(form).toBeHidden();

  const status = async (id: string | null) =>
    ((await (await request.get(`${s.api}/api/v1/sessions/${id}`, { headers: auth })).json()) as { status: string }).status;
  expect(await status(old.session_id)).toBe("ORPHAN");
  expect(await status(fresh.session_id)).toBe("ACTIVE");
});
