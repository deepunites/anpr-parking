import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/errors";
import { REDACTED, redactText, scrubEvent } from "./scrub";

const JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
const KEY = "cam_a1_k7Qm3ZxV9pL2sN8bT4wY6uH1jR5cE0dFgA3B";
const PLATE = "01A123BC";

describe("sentry before_send (фаза 7 §6)", () => {
  it("номер и JWT не уходят ни в одном поле события", () => {
    const event = scrubEvent({
      message: `failed for ${PLATE} with ${JWT}`,
      logentry: { message: "plate %s", params: [PLATE] },
      extra: { plate: PLATE, camera_key: KEY, safe: "ok", nested: { plate_number: PLATE } },
      contexts: { vehicle: { plate: PLATE }, trace: { trace_id: "abc" } },
      breadcrumbs: [{ category: "fetch", message: `GET /vehicles?plate=${PLATE}`, data: { authorization: JWT } }],
      exception: { values: [{ type: "Error", value: `boom ${PLATE}` }] },
      tags: { lot_id: "l1", token: JWT },
      user: { id: "u1", email: "user@example.com", ip_address: "1.2.3.4", tenant_id: "t1", role: "OPERATOR" },
      request: {
        url: `https://app/vehicles?plate=${PLATE}`,
        query_string: `plate=${PLATE}`,
        headers: { Authorization: `Bearer ${JWT}`, "X-Camera-Key": KEY, Cookie: "s=1", Accept: "*" },
        cookies: { s: "1" },
        data: { plate_number: PLATE, amount: 5000 },
      },
    });
    expect(event).not.toBeNull();
    const dumped = JSON.stringify(event);
    for (const forbidden of [PLATE, JWT, KEY, "user@example.com", "1.2.3.4", "s=1"]) expect(dumped).not.toContain(forbidden);
    expect(event!.extra).toMatchObject({ plate: REDACTED, camera_key: REDACTED, safe: "ok", nested: { plate_number: REDACTED } });
    expect(event!.request!.headers).toEqual({ Authorization: REDACTED, "X-Camera-Key": REDACTED, Cookie: REDACTED, Accept: "*" });
    expect(event!.request).not.toHaveProperty("data");
    expect(event!.request).not.toHaveProperty("cookies");
    expect(event!.user).toEqual({ id: "u1", tenant_id: "t1", role: "OPERATOR" });
    expect(event!.tags).toEqual({ lot_id: "l1", token: REDACTED });
  });

  it("ожидаемые ошибки API (4xx, доменные коды) не отправляются, 5xx и баги — отправляются", () => {
    expect(scrubEvent({ message: "x" }, { originalException: new ApiError(409, { error: { code: "DUPLICATE", message: "dup", correlation_id: "cid" } }) })).toBeNull();
    expect(scrubEvent({ message: "x" }, { originalException: new ApiError(422, { error: { code: "VALIDATION_ERROR", message: "bad", correlation_id: "cid" } }) })).toBeNull();
    expect(scrubEvent({ message: "x" }, { originalException: new ApiError(502, { error: { code: "UPSTREAM", message: "gw", correlation_id: "cid" } }) })).not.toBeNull();
    expect(scrubEvent({ message: "x" }, { originalException: new TypeError("undefined is not a function") })).not.toBeNull();
    expect(scrubEvent({ message: "x" })).not.toBeNull();
  });

  it("redactText: форматы номеров, Bearer, длинные ключи; UUID и обычный текст не трогает", () => {
    expect(redactText("plate 01 A 123 BC seen")).toBe(`plate ${REDACTED} seen`);
    expect(redactText("10123AAA")).toBe(REDACTED);
    expect(redactText(`Bearer ${JWT}`)).toBe(`Bearer ${REDACTED}`);
    expect(redactText("uuid 0af7651916cd43dd8448eb211c80319c ok")).toBe("uuid 0af7651916cd43dd8448eb211c80319c ok");
    expect(redactText("session 2f5cb769-e259-440f-be47-2f8251729dd1 closed")).toBe("session 2f5cb769-e259-440f-be47-2f8251729dd1 closed");
  });
});
