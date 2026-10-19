import { describe, expect, it } from "vitest";
import { DEFAULT_FORM, formFromRules, isActiveAt, rulesFromForm, zonedInputToIso, zonedNowInput, type TariffForm } from "./tariff";

const base = (): TariffForm => structuredClone(DEFAULT_FORM);

describe("rulesFromForm", () => {
  it("turns sums into tiyins like the starter tariff of the server", () => {
    expect(rulesFromForm(base())).toEqual({
      grace_minutes: 15,
      first_hour_price: 500_000,
      price_per_hour: 300_000,
      rounding_mode: "UP",
      rounding_step_minutes: 15,
      first_hour_prorated: false,
      amount_step: null,
      free_visits: null,
      repeat_visit_price: null,
      daily_max: 3_000_000,
      overnight: null,
      weekday_overrides: null,
    });
  });

  it("per-minute first hour and the total rounded to 1000 sum (MediaPark: no small change)", () => {
    const form = { ...base(), firstProrated: true, amountStep: "1000", step: "1" };
    const rules = rulesFromForm(form)!;
    expect(rules["first_hour_prorated"]).toBe(true);
    expect(rules["amount_step"]).toBe(100_000);
    expect(formFromRules(rules)).toMatchObject({ firstProrated: true, amountStep: "1000" });
    expect(rulesFromForm({ ...form, amountStep: "0" })).toBeNull();
  });

  it("free minutes only for the first visits in 24 hours, then a flat price (MediaPark loophole)", () => {
    const rules = rulesFromForm({ ...base(), freeVisits: "2", repeatPrice: "8000" })!;
    expect(rules["free_visits"]).toBe(2);
    expect(rules["repeat_visit_price"]).toBe(800_000);
    expect(formFromRules(rules)).toMatchObject({ freeVisits: "2", repeatPrice: "8000" });
    expect(rulesFromForm({ ...base(), freeVisits: "2", repeatPrice: "" })).toBeNull(); // только вместе
    expect(rulesFromForm({ ...base(), freeVisits: "", repeatPrice: "8000" })).toBeNull();
    expect(rulesFromForm({ ...base(), freeVisits: "1.5", repeatPrice: "8000" })).toBeNull();
  });

  it("supports night price, free and special days, and an empty daily cap", () => {
    const form = base();
    form.dailyMax = "";
    form.night = true;
    form.nightPrice = "1 000";
    form.days[5] = { kind: "free", firstHour: "", perHour: "" };
    form.days[6] = { kind: "custom", firstHour: "", perHour: "2500,5" };
    const rules = rulesFromForm(form)!;
    expect(rules["daily_max"]).toBeNull();
    expect(rules["overnight"]).toEqual({ start: "22:00", end: "07:00", price_per_hour: 100_000 });
    expect(rules["weekday_overrides"]).toEqual({
      "5": { free: true, price_per_hour: null, first_hour_price: null, daily_max: null },
      "6": { free: false, price_per_hour: 250_050, first_hour_price: null, daily_max: null },
    });
  });

  it("rejects what the server would reject", () => {
    for (const patch of [{ firstHour: "" }, { perHour: "-1" }, { grace: "abc" }, { step: "0" }, { dailyMax: "x" }]) {
      expect(rulesFromForm({ ...base(), ...patch })).toBeNull();
    }
    expect(rulesFromForm({ ...base(), night: true, nightStart: "25:00" })).toBeNull();
    const day = base();
    day.days[0] = { kind: "custom", firstHour: "", perHour: "" };
    expect(rulesFromForm(day)).toBeNull();
  });

  it("keeps fields the screen does not edit (holidays) from the current version", () => {
    const current = { ...rulesFromForm(base())!, holiday: { free: true, price_per_hour: null, first_hour_price: null, daily_max: null } };
    expect(rulesFromForm(base(), current)!["holiday"]).toEqual(current.holiday);
  });
});

describe("formFromRules", () => {
  it("round-trips a published version", () => {
    const form = base();
    form.night = true;
    form.days[6] = { kind: "custom", firstHour: "4000", perHour: "2000" };
    const rules = rulesFromForm(form)!;
    expect(rulesFromForm(formFromRules(rules))).toEqual(rules);
  });

  it("falls back to the starter values when the lot has no tariff yet", () => {
    expect(formFromRules(null)).toEqual(DEFAULT_FORM);
  });
});

it("checks validity windows", () => {
  const v = { valid_from: "2026-10-10T09:00:00+05:00", valid_to: "2026-10-11T09:00:00+05:00" };
  expect(isActiveAt(v, Date.parse("2026-10-10T12:00:00+05:00"))).toBe(true);
  expect(isActiveAt(v, Date.parse("2026-10-11T09:00:00+05:00"))).toBe(false);
  expect(isActiveAt({ ...v, valid_to: null }, Date.parse("2030-01-01T00:00:00Z"))).toBe(true);
});

describe("details found in review", () => {
  it("reads thousands separators and decimal commas", () => {
    expect(rulesFromForm({ ...base(), firstHour: "5,000", dailyMax: "12,500,000" })).toMatchObject({ first_hour_price: 500_000, daily_max: 1_250_000_000 });
    expect(rulesFromForm({ ...base(), perHour: "2500,5" })).toMatchObject({ price_per_hour: 250_050 });
  });

  it("keeps a per-day daily cap that the screen does not edit", () => {
    const current = { ...rulesFromForm(base())!, weekday_overrides: { "6": { free: false, price_per_hour: null, first_hour_price: null, daily_max: 900_000 } } };
    const form = formFromRules(current);
    expect(form.days[6]!.kind).toBe("custom");
    expect(rulesFromForm(form, current)!["weekday_overrides"]).toEqual({ "6": { free: false, price_per_hour: null, first_hour_price: null, daily_max: 900_000 } });
  });

  it("treats the scheduled time as the lot's wall clock, not the browser's", () => {
    expect(zonedInputToIso("2026-10-10T10:00", "Asia/Tashkent")).toBe("2026-10-10T05:00:00.000Z");
    expect(zonedInputToIso("2026-10-10T10:00", "Europe/Moscow")).toBe("2026-10-10T07:00:00.000Z");
    expect(zonedInputToIso("not a date", "Asia/Tashkent")).toBeNull();
    expect(zonedNowInput("Asia/Tashkent", Date.parse("2026-10-10T05:30:59Z"))).toBe("2026-10-10T10:30");
  });
});
