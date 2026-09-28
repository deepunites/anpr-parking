import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, refreshAccessToken } from "./client";
import { useAuthStore } from "@/auth/store";

/** Параллельные 401 → один refresh (дедупликация), оба запроса повторяются с новым токеном. */
describe("token refresh", () => {
  const calls: { url: string; auth: string | null }[] = [];
  let refreshCount = 0;

  beforeEach(() => {
    calls.length = 0;
    refreshCount = 0;
    useAuthStore.getState().setSession("old-token", 900);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(input, init);
        const auth = request.headers.get("Authorization");
        calls.push({ url: request.url, auth });
        if (request.url.endsWith("/api/v1/auth/refresh")) {
          refreshCount += 1;
          await new Promise((r) => setTimeout(r, 5));
          return new Response(JSON.stringify({ access_token: "new-token", expires_in: 900, token_type: "bearer" }), { status: 200, headers: { "content-type": "application/json" } });
        }
        if (auth === "Bearer old-token") {
          return new Response(JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "expired", correlation_id: "c-1" } }), { status: 401, headers: { "content-type": "application/json" } });
        }
        return new Response(JSON.stringify([]), { status: 200, headers: { "content-type": "application/json" } });
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("deduplicates concurrent refreshes and retries once", async () => {
    const [a, b] = await Promise.all([api.GET("/api/v1/lots"), api.GET("/api/v1/cameras")]);
    expect(a.response.status).toBe(200);
    expect(b.response.status).toBe(200);
    expect(refreshCount).toBe(1);
    expect(useAuthStore.getState().accessToken).toBe("new-token");
    const retried = calls.filter((c) => c.auth === "Bearer new-token");
    expect(retried.map((c) => new URL(c.url).pathname).sort()).toEqual(["/api/v1/cameras", "/api/v1/lots"]);
  });

  it("clears the session when refresh fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    expect(await refreshAccessToken()).toBe(false);
    expect(useAuthStore.getState().accessToken).toBeNull();
  });
});
