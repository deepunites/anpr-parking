import { create } from "zustand";

/* Тема и масштаб — удобство конкретного рабочего места (будка со слепящим солнцем или ночная смена):
   хранятся в localStorage этого устройства, к серверу отношения не имеют. Чтение/запись обёрнуты в try:
   приватное окно или запрет на хранение не должны ломать панель. */
export type ThemeMode = "light" | "dark" | "auto";
export type Scale = "md" | "lg";

const KEY = "parking.ui";

function load(): { theme: ThemeMode; scale: Scale } {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const v = JSON.parse(raw) as Partial<{ theme: ThemeMode; scale: Scale }>;
      return { theme: v.theme ?? "auto", scale: v.scale ?? "md" };
    }
  } catch {
    /* нет хранилища — системные настройки */
  }
  return { theme: "auto", scale: "md" };
}

export function applyTheme(theme: ThemeMode, scale: Scale): void {
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  if (scale === "md") root.removeAttribute("data-scale");
  else root.setAttribute("data-scale", scale);
}

interface ThemeState {
  theme: ThemeMode;
  scale: Scale;
  setTheme: (theme: ThemeMode) => void;
  setScale: (scale: Scale) => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  ...load(),
  setTheme: (theme) => {
    set({ theme });
    persist(get());
  },
  setScale: (scale) => {
    set({ scale });
    persist(get());
  },
}));

function persist(s: ThemeState): void {
  applyTheme(s.theme, s.scale);
  try {
    localStorage.setItem(KEY, JSON.stringify({ theme: s.theme, scale: s.scale }));
  } catch {
    /* нет хранилища — тема живёт до перезагрузки */
  }
}

/** Вызывается один раз при старте до первого рендера, чтобы не было «вспышки» не той темы. */
export function bootTheme(): void {
  const s = useThemeStore.getState();
  applyTheme(s.theme, s.scale);
}
