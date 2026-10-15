/** Даты — в таймзоне площадки (§6); суммы UI не форматирует: `amount.formatted` приходит с бэкенда. */
export function formatTime(iso: string | null | undefined, timeZone: string | undefined, lang: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat(lang === "uz" ? "uz-UZ" : "ru-RU", {
      timeZone: timeZone || "Asia/Tashkent",
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  } catch {
    return date.toISOString();
  }
}

/** Время события коротко: «21:17» сегодня, иначе «14.10 21:17» (часы площадки). */
export function formatClock(iso: string | null | undefined, timeZone: string | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const tz = timeZone || "Asia/Tashkent";
  const day = (d: Date) => new Intl.DateTimeFormat("ru-RU", { timeZone: tz, day: "2-digit", month: "2-digit" }).format(d);
  const time = new Intl.DateTimeFormat("ru-RU", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(date);
  return day(date) === day(new Date(now)) ? time : `${day(date)} ${time}`;
}

/** Сколько минут машина стоит: от въезда до выезда, а пока не выехала — до сейчас. */
export function stayMinutes(entry: string | null | undefined, exit: string | null | undefined, now = Date.now()): number | null {
  if (!entry) return null;
  const from = new Date(entry).getTime();
  const to = exit ? new Date(exit).getTime() : now;
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  return Math.max(0, Math.floor((to - from) / 60_000));
}

export function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—";
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h > 0 ? `${h} ч ${m} мин` : `${m} мин`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}

/** Суммы смен приходят целыми тийинами; для показа — сумы с разделителями (расчёт на сервере, здесь только вид). */
export function formatMinor(minor: number | null | undefined, lang = "ru"): string {
  if (minor === null || minor === undefined) return "—";
  const sum = Math.round(minor) / 100;
  return `${sum.toLocaleString(lang === "uz" ? "uz-UZ" : "ru-RU", { maximumFractionDigits: 2 })} ${lang === "uz" ? "so‘m" : "сум"}`;
}
