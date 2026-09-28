/**
 * Локализация без зависимостей (~1 КБ вместо i18next): русский и узбекский словари, `useTranslation()`
 * с тем же контрактом `{ t, i18n.language }`, что и react-i18next. Язык — клиентское состояние (Zustand).
 */
import { create } from "zustand";
import { ru, type Dict } from "./ru";
import { uz } from "./uz";

export type Lang = "ru" | "uz";
export const LANG_KEY = "anpr.lang";
const DICTS: Record<Lang, Dict> = { ru, uz };

export function readLang(): Lang {
  try {
    return localStorage.getItem(LANG_KEY) === "uz" ? "uz" : "ru";
  } catch {
    return "ru";
  }
}

interface LangState {
  lang: Lang;
  setLang: (lang: Lang) => void;
}

export const useLangStore = create<LangState>((set) => ({
  lang: readLang(),
  setLang: (lang) => {
    try {
      localStorage.setItem(LANG_KEY, lang);
    } catch {
      /* приватный режим */
    }
    document.documentElement.lang = lang;
    set({ lang });
  },
}));

export function setLang(lang: Lang): void {
  useLangStore.getState().setLang(lang);
}

function lookup(dict: Dict, key: string): string | undefined {
  let node: unknown = dict;
  for (const part of key.split(".")) {
    if (typeof node !== "object" || node === null || !(part in node)) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

export function translate(lang: Lang, key: string, options?: { defaultValue?: string }): string {
  return lookup(DICTS[lang], key) ?? lookup(DICTS.ru, key) ?? options?.defaultValue ?? key;
}

export function useTranslation(): { t: (key: string, options?: { defaultValue?: string }) => string; i18n: { language: Lang } } {
  const lang = useLangStore((s) => s.lang);
  return { t: (key, options) => translate(lang, key, options), i18n: { language: lang } };
}

/** Подстановка `{name}` в переведённую строку. */
export function fmt(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m));
}

/** Форма слова по числу: «1 площадка, 2 площадки, 5 площадок». Формы через «|»; одна форма — без склонения (uz). */
export function plural(n: number, forms: string): string {
  const f = forms.split("|");
  if (f.length < 3) return f[0] ?? "";
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return f[0]!;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return f[1]!;
  return f[2]!;
}
