import { expect, test } from "@playwright/test";
import { loginUi, stack } from "./stack";

/** Gate: обрыв WebSocket → индикатор «устарело» → переподключение → состояние перезапрошено REST'ом. */
test("websocket drop shows stale indicator, reconnects and refetches state", async ({ page }) => {
  const s = stack();
  const sockets: Array<{ close: (opts?: { code?: number; reason?: string }) => Promise<void> }> = [];
  await page.routeWebSocket(/\/ws\/v1\/lots\//, (ws) => {
    const server = ws.connectToServer();
    ws.onMessage((message) => server.send(message));
    server.onMessage((message) => ws.send(message));
    server.onClose((code, reason) => ws.close({ code: code ?? 1006, reason: reason ?? "" }));
    ws.onClose((code, reason) => server.close({ code: code ?? 1000, reason: reason ?? "" }));
    sockets.push(ws); // страница видит обрыв, когда закрывается её сторона
  });
  await loginUi(page, s.users["operator"]!);
  await page.getByTestId("lot-select").selectOption(s.lot_id);
  const indicator = page.getByTestId("connection-indicator");
  await expect(indicator).toHaveAttribute("data-status", "open");
  await expect(indicator).toHaveAttribute("data-stale", "0");
  expect(sockets.length).toBeGreaterThanOrEqual(1);

  const occupancyRequests: string[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/occupancy")) occupancyRequests.push(req.url());
  });
  const before = occupancyRequests.length;

  await sockets[sockets.length - 1]!.close({ code: 1012, reason: "server restart" }); // сервер «пропал»
  await expect(indicator).toHaveAttribute("data-stale", "1");
  await expect(indicator).toContainText("устаревшими");
  await expect(indicator).toHaveAttribute("data-status", "open", { timeout: 20_000 }); // бэкофф ≤ 2 с на первой попытке
  await expect(indicator).toHaveAttribute("data-stale", "0");
  await expect.poll(() => occupancyRequests.length, { timeout: 10_000 }).toBeGreaterThan(before); // перезапрос состояния, не «возобновление потока»
  expect(sockets.length).toBeGreaterThanOrEqual(2);
});
