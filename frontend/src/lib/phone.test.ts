import { describe, expect, it } from "vitest";
import { formatPhone, isEmail, normalizePhone } from "./phone";

describe("normalizePhone", () => {
  it.each([
    ["+998 90 123-45-67", "+998901234567"],
    ["998901234567", "+998901234567"],
    ["90 123 45 67", "+998901234567"],
    ["(90) 123-45-67", "+998901234567"],
    ["00998901234567", "+998901234567"],
    ["+7 916 123-45-67", "+79161234567"],
  ])("%s → %s", (raw, want) => expect(normalizePhone(raw)).toBe(want));
  it.each(["", "123", "+99890abc", "12345678901234567"])("rejects %s", (raw) => expect(normalizePhone(raw)).toBeNull());
});

describe("formatPhone / isEmail", () => {
  it("formats Uzbek numbers", () => expect(formatPhone("+998901234567")).toBe("+998 90 123-45-67"));
  it("keeps foreign numbers", () => expect(formatPhone("+79161234567")).toBe("+79161234567"));
  it("detects e-mail", () => {
    expect(isEmail("a.b@company.uz")).toBe(true);
    expect(isEmail("+998901234567")).toBe(false);
  });
});
