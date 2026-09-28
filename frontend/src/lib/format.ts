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
