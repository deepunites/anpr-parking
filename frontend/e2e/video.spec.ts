import { expect, test } from "@playwright/test";
import { cameraEvent, loginUi, masked, shown, stack, uniquePlate } from "./stack";

/** «Видеозапись» камеры: адрес, логин и пароль для записи на сервер площадки — камере без своей стрелы (выездной,
    когда стрела одна). Пароль в форму не возвращается; «Не записывать» убирает доступ. Камера — своя, A1 не трогаем. */
test("camera video access is saved and removed", async ({ page }) => {
  const s = stack();
  await loginUi(page, s.users["tenant_admin"]!);
  await page.getByTestId("lot-select").selectOption("00000000-0000-4000-8000-00000000a101");
  await page.goto("/devices");
  const code = `VID_${Date.now() % 100000}`;
  await page.getByRole("button", { name: "Добавить камеру" }).click();
  await page.getByTestId("camera-name").fill(`Выезд ${code}`);
  await page.getByTestId("camera-code").fill(code);
  await page.getByTestId("camera-submit").click();
  await page.getByTestId("key-done").click();
  const row = page.getByTestId("camera-row").and(page.locator(`[data-code="${code}"]`));
  await expect(row.getByTestId("camera-video")).toHaveAttribute("data-on", "0");

  await row.getByTestId("camera-video").click();
  const form = page.getByTestId("camera-video-form");
  await expect(form.getByTestId("video-off")).toHaveCount(0); // выключать пока нечего
  await form.getByTestId("video-endpoint").fill("http://10.10.1.251");
  await form.getByTestId("video-username").fill("video");
  await expect(form.getByTestId("video-submit")).toBeDisabled(); // без пароля — нельзя
  await form.getByTestId("video-password").fill("Video-Pass-1");
  await form.getByTestId("video-submit").click();
  await expect(form).toBeHidden();
  await expect(row.getByTestId("camera-video")).toHaveAttribute("data-on", "1");

  await row.getByTestId("camera-video").click();
  await expect(page.getByTestId("video-endpoint")).toHaveValue("http://10.10.1.251");
  await expect(page.getByTestId("video-password")).toHaveValue(""); // сохранённый пароль не показывается
  await page.getByTestId("video-off").click();
  await expect(page.getByTestId("camera-video-form")).toBeHidden();
  await expect(row.getByTestId("camera-video")).toHaveAttribute("data-on", "0");
});

/** Сессия по номеру → ролик въезда из записи камеры. На e2e-стенде записи нет, поэтому список роликов и сам ролик —
    подставные (сервер с записью проверяют тесты бэкенда: tests/api/test_session_video.py). */
test("session panel plays the entry video", async ({ page, request }) => {
  const s = stack();
  const clip = "/api/v1/video/clips/e2e-signed-link";
  await page.route("**/api/v1/sessions/*/videos", async (route) => {
    const at = new Date().toISOString();
    await route.fulfill({
      json: {
        enabled: true,
        videos: [{ kind: "ENTRY", camera_id: "c", event_time: at, start: at, end: at, available: true, url: clip, expires_in: 300 }],
      },
    });
  });
  await page.route(`**${clip}`, (route) => route.fulfill({ status: 200, contentType: "video/mp4", body: "" }));
  await loginUi(page, s.users["manager"]!);
  await page.getByTestId("lot-select").selectOption(s.lot_id);

  const plate = uniquePlate();
  await cameraEvent(request, s, s.camera_keys.entry, { camera_code: "ENTRY_01", event_type: "ENTRY", plate_number: plate });
  const row = page.getByTestId("list-active").locator(`[data-plate='${masked(plate)}']`);
  await row.click();
  await expect(page.getByTestId("session-plate")).toHaveText(shown(masked(plate)));

  const videos = page.getByTestId("session-videos");
  await expect(videos.locator("[data-kind='EXIT']")).toHaveCount(0); // машина ещё на парковке
  await videos.locator("[data-testid='session-video'][data-kind='ENTRY']").click();
  await expect(page.getByTestId("session-video-player")).toHaveAttribute("src", clip);
});
