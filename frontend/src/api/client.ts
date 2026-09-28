/**
 * Типизированный клиент (openapi-fetch + сгенерированная схема). Access-токен из памяти, 401 → один общий refresh
 * на все параллельные запросы (дедупликация), после refresh запрос повторяется один раз; неудачный refresh —
 * выход. Секретов в бандле нет: базовый URL относительный (dev/preview — прокси Vite, prod — nginx).
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import { useAuthStore } from "@/auth/store";
import { toApiError } from "./errors";

const ORIGIN = typeof window !== "undefined" ? window.location.origin : "";
const REFRESH_PATH = "/api/v1/auth/refresh";
let refreshInFlight: Promise<boolean> | null = null;

export async function refreshAccessToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${ORIGIN}${REFRESH_PATH}`, { method: "POST", credentials: "include" });
      if (!response.ok) {
        useAuthStore.getState().clear();
        return false;
      }
      const body = (await response.json()) as { access_token: string; expires_in: number };
      useAuthStore.getState().setSession(body.access_token, body.expires_in);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export function tokenNeedsRefresh(): boolean {
  const { accessToken, expiresAt } = useAuthStore.getState();
  return accessToken === null || expiresAt === null || expiresAt - Date.now() < 30_000;
}

const auth: Middleware = {
  async onRequest({ request }) {
    if (request.url.includes("/api/v1/auth/login") || request.url.includes(REFRESH_PATH)) return request;
    if (tokenNeedsRefresh() && useAuthStore.getState().accessToken !== null) await refreshAccessToken();
    const token = useAuthStore.getState().accessToken;
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
  async onResponse({ request, response }) {
    if (response.status !== 401 || request.url.includes(REFRESH_PATH) || request.url.includes("/auth/login")) return response;
    if (request.headers.get("x-retried") === "1") return response;
    const ok = await refreshAccessToken();
    if (!ok) return response;
    const retry = new Request(request, { headers: new Headers(request.headers) });
    retry.headers.set("Authorization", `Bearer ${useAuthStore.getState().accessToken ?? ""}`);
    retry.headers.set("x-retried", "1");
    return fetch(retry);
  },
};

export const api = createClient<paths>({
  baseUrl: ORIGIN,
  credentials: "include",
  fetch: (request) => globalThis.fetch(request), // глобальный fetch на момент вызова (тестируемость, полифиллы)
});
api.use(auth);

/** Разворачивает ответ openapi-fetch в данные или бросает ApiError с code/correlation_id. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) throw toApiError(result.response.status, result.error);
  return result.data as T;
}
