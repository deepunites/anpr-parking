import { useCallback, useSyncExternalStore } from "react";

/** Совпадает ли медиазапрос сейчас (ширина экрана, тип указателя) — с подпиской на изменения: телефон кассира
 *  получает другую раскладку, а не ту же страницу в узкой колонке. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (typeof window.matchMedia !== "function") return () => undefined;
      const mq = window.matchMedia(query);
      mq.addEventListener("change", onChange);
      return () => mq.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches, // jsdom без matchMedia
    () => false,
  );
}

/** Ширина как у телефона (уже 640 px — брейкпоинт sm Tailwind). */
export const PHONE = "(max-width: 639px)";
/** Ширина, на которой «Живая парковка» помещается в три колонки (брейкпоинт xl Tailwind). */
export const WIDE = "(min-width: 1280px)";
