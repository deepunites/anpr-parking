import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
import type { APIRequestContext, Page } from "@playwright/test";

export interface Stack {
  api: string;
  users: Record<string, { email: string; password: string; user_id: string }>;
  camera_keys: { entry: string; exit: string };
  lot_id: string;
  tenant_a: string;
}

export function stack(): Stack {
  return JSON.parse(fs.readFileSync(path.resolve(__dirname, ".stack.json"), "utf8")) as Stack;
}

export async function loginUi(page: Page, user: { email: string; password: string }): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("E-mail").fill(user.email);
  await page.getByLabel("Пароль").fill(user.password);
  await page.getByTestId("login-submit").click();
  await page.waitForURL(/\/live$/);
}

export async function apiToken(request: APIRequestContext, s: Stack, user: { email: string; password: string }): Promise<string> {
  const response = await request.post(`${s.api}/api/v1/auth/login`, { data: { email: user.email, password: user.password } });
  if (!response.ok()) throw new Error(`login failed: ${response.status()}`);
  return ((await response.json()) as { access_token: string }).access_token;
}

export function uniquePlate(prefix = "01E"): string {
  const n = Math.floor(Math.random() * 1_000_000).toString(36).toUpperCase().padStart(4, "0").slice(0, 4);
  return `${prefix}${n}AA`.slice(0, 8);
}

export async function cameraEvent(
  request: APIRequestContext,
  s: Stack,
  key: string,
  body: { camera_code: string; event_type: "ENTRY" | "EXIT"; plate_number: string; event_time?: string },
): Promise<{ status: string; session_id: string | null }> {
  const response = await request.post(`${s.api}/api/v1/camera/events`, {
    headers: { "X-Camera-Key": key },
    data: { confidence: 96, direction: body.event_type === "ENTRY" ? "IN" : "OUT", event_time: body.event_time ?? new Date().toISOString(), ...body },
  });
  if (!response.ok()) throw new Error(`camera event failed: ${response.status()} ${await response.text()}`);
  return (await response.json()) as { status: string; session_id: string | null };
}

/** Номера в API маскируются (`01A123AA` → `01A***AA`): UI показывает маску, полный номер видит только корректировка. */
/** Как табличка показывает номер в интерфейсе v2: группы через пробел + префикс UZ (компонент Plate). */
export function shown(plate: string): string {
  const p = plate.replace(/\s+/g, "").toUpperCase();
  const m = /^(\d{2})([A-Z*])([\d*]{3})([A-Z*]{2})$/.exec(p);
  return "UZ " + (m ? m.slice(1).join(" ") : p);
}

export function masked(plate: string): string {
  return plate.length >= 6 ? plate.slice(0, 3) + "*".repeat(plate.length - 5) + plate.slice(-2) : plate[0] + "*".repeat(plate.length - 1);
}
