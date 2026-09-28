/** Номер ТС на клиенте — та же нормализация, что на сервере (`shared/plate.py`): регистр, разделители,
    кириллические омоглифы → латиница. Клиент только помогает вводить; истина — сервер (422 INVALID_PLATE). */

const CYR = "АВЕКМНОРСТУХ";
const LAT = "ABEKMHOPCTYX";

export type PlateFormat = "UZ_STANDARD" | "UZ_LEGAL" | "FOREIGN";

export function normalizePlate(raw: string): string {
  let out = "";
  for (const ch of raw.toUpperCase()) {
    if (/[\s\-._/·]/.test(ch)) continue;
    const i = CYR.indexOf(ch);
    out += i >= 0 ? LAT[i]! : ch;
  }
  return out;
}

export function detectFormat(value: string): PlateFormat | null {
  if (/^\d{2}[A-Z]\d{3}[A-Z]{2}$/.test(value)) return "UZ_STANDARD";
  if (/^\d{5}[A-Z]{3}$/.test(value)) return "UZ_LEGAL";
  if (/^[A-Z0-9]{4,12}$/.test(value)) return "FOREIGN";
  return null;
}

/** Символы, которых не бывает в номере (например, кириллические Ж, Б) — подсказка оператору. */
export function invalidChars(value: string): string[] {
  return [...new Set(value.split("").filter((c) => !/[A-Z0-9]/.test(c)))];
}

/** Разбивка для показа: 01 A 777 AA / 01 777 AAA; для незавершённого узбекского номера — по уже введённым группам. */
export function formatPlate(value: string): string {
  const std = /^(\d{1,2})([A-Z]?)(\d{0,3})([A-Z]{0,2})$/.exec(value);
  if (std && value.length <= 8 && (value.length < 3 || /^\d{2}[A-Z]/.test(value))) return [std[1], std[2], std[3], std[4]].filter(Boolean).join(" ");
  const legal = /^(\d{2})(\d{0,3})([A-Z]{0,3})$/.exec(value);
  if (legal && value.length <= 8) return [legal[1], legal[2], legal[3]].filter(Boolean).join(" ");
  return value;
}

/** Похоже ли на начало узбекского номера — чтобы подсказывать «ещё N символов», а не «иностранный». */
export function uzProgress(value: string): { expected: PlateFormat; missing: number } | null {
  if (/^\d{2}[A-Z]\d{0,3}[A-Z]{0,2}$/.test(value) && value.length < 8) return { expected: "UZ_STANDARD", missing: 8 - value.length };
  if (/^\d{3,5}[A-Z]{0,3}$/.test(value) && value.length < 8) return { expected: "UZ_LEGAL", missing: 8 - value.length };
  if (/^\d{1,2}$/.test(value)) return { expected: "UZ_STANDARD", missing: 8 - value.length };
  return null;
}
