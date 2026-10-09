/* Тариф площадки на экране «Тариф»: правила версии (суммы в тийинах, как в tariff_plan_version.rules) ↔ форма
   (суммы в сумах строками, как их вводят). Неизвестные экрану поля текущей версии (праздники) сохраняются. */

export type RoundingMode = "UP" | "NEAREST" | "DOWN";
export type DayKind = "base" | "free" | "custom";
export interface DayForm {
  kind: DayKind;
  firstHour: string;
  perHour: string;
}
export interface TariffForm {
  grace: string;
  firstHour: string;
  perHour: string;
  step: string;
  mode: RoundingMode;
  dailyMax: string;
  night: boolean;
  nightStart: string;
  nightEnd: string;
  nightPrice: string;
  days: DayForm[]; // 0 = понедельник … 6 = воскресенье, как weekday_overrides
}
export type Rules = Record<string, unknown>;

const TIYIN = 100;

export const DEFAULT_FORM: TariffForm = {
  grace: "15",
  firstHour: "5000",
  perHour: "3000",
  step: "15",
  mode: "UP",
  dailyMax: "30000",
  night: false,
  nightStart: "22:00",
  nightEnd: "07:00",
  nightPrice: "1000",
  days: Array.from({ length: 7 }, () => ({ kind: "base" as DayKind, firstHour: "", perHour: "" })),
};

const sum = (minor: unknown): string => (typeof minor === "number" ? String(minor / TIYIN) : "");

export function formFromRules(rules: Rules | null | undefined): TariffForm {
  if (!rules) return structuredClone(DEFAULT_FORM);
  const overnight = rules["overnight"] as { start?: string; end?: string; price_per_hour?: number } | null | undefined;
  const overrides = (rules["weekday_overrides"] ?? {}) as Record<string, { free?: boolean; price_per_hour?: number | null; first_hour_price?: number | null }>;
  return {
    grace: String(rules["grace_minutes"] ?? DEFAULT_FORM.grace),
    firstHour: sum(rules["first_hour_price"]),
    perHour: sum(rules["price_per_hour"]),
    step: String(rules["rounding_step_minutes"] ?? DEFAULT_FORM.step),
    mode: (rules["rounding_mode"] as RoundingMode | undefined) ?? "UP",
    dailyMax: sum(rules["daily_max"]),
    night: Boolean(overnight),
    nightStart: overnight?.start ?? DEFAULT_FORM.nightStart,
    nightEnd: overnight?.end ?? DEFAULT_FORM.nightEnd,
    nightPrice: overnight ? sum(overnight.price_per_hour) : DEFAULT_FORM.nightPrice,
    days: Array.from({ length: 7 }, (_, d) => {
      const o = overrides[String(d)];
      if (!o) return { kind: "base" as DayKind, firstHour: "", perHour: "" };
      return { kind: (o.free ? "free" : "custom") as DayKind, firstHour: sum(o.first_hour_price), perHour: sum(o.price_per_hour) };
    }),
  };
}

/** Сумма в сумах → тийины; пусто → null; не число или отрицательное → undefined (ошибка формы). */
function toMinor(value: string): number | null | undefined {
  const v = value.trim().replace(/\s/g, "").replace(",", ".");
  if (v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return undefined;
  return Math.round(n * TIYIN);
}

const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Правила из формы или null, если форма заполнена неверно (пустая обязательная цена, отрицательное, не время). */
export function rulesFromForm(form: TariffForm, base?: Rules | null): Rules | null {
  const grace = Number(form.grace);
  const step = Number(form.step);
  const firstHour = toMinor(form.firstHour);
  const perHour = toMinor(form.perHour);
  const dailyMax = toMinor(form.dailyMax);
  if (!Number.isInteger(grace) || grace < 0 || grace > 1440 || !Number.isInteger(step) || step < 1) return null;
  if (firstHour == null || perHour == null || dailyMax === undefined) return null;
  let overnight: Rules | null = null;
  if (form.night) {
    const price = toMinor(form.nightPrice);
    if (price == null || !TIME.test(form.nightStart) || !TIME.test(form.nightEnd) || form.nightStart === form.nightEnd) return null;
    overnight = { start: form.nightStart, end: form.nightEnd, price_per_hour: price };
  }
  const overrides: Record<string, Rules> = {};
  for (const [d, day] of form.days.entries()) {
    if (day.kind === "base") continue;
    if (day.kind === "free") {
      overrides[String(d)] = { free: true, price_per_hour: null, first_hour_price: null, daily_max: null };
      continue;
    }
    const dayPer = toMinor(day.perHour);
    const dayFirst = toMinor(day.firstHour);
    if (dayPer === undefined || dayFirst === undefined || (dayPer === null && dayFirst === null)) return null;
    overrides[String(d)] = { free: false, price_per_hour: dayPer, first_hour_price: dayFirst, daily_max: null };
  }
  return {
    ...(base ?? {}),
    grace_minutes: grace,
    first_hour_price: firstHour,
    price_per_hour: perHour,
    rounding_mode: form.mode,
    rounding_step_minutes: step,
    daily_max: dailyMax,
    overnight,
    weekday_overrides: Object.keys(overrides).length > 0 ? overrides : null,
  };
}

/** Действует ли версия в момент at (valid_from ≤ at < valid_to). */
export function isActiveAt(v: { valid_from: string; valid_to: string | null }, at: number): boolean {
  return Date.parse(v.valid_from) <= at && (v.valid_to === null || at < Date.parse(v.valid_to));
}
