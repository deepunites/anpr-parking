import { create } from "zustand";

/** Выбранная площадка — клиентское состояние UI (Zustand), список площадок — серверное (TanStack Query). */
interface LotState {
  lotId: string | null;
  setLotId: (id: string) => void;
}

const KEY = "anpr.lot";

export const useLotStore = create<LotState>((set) => ({
  lotId: (() => {
    try {
      return localStorage.getItem(KEY);
    } catch {
      return null;
    }
  })(),
  setLotId: (id) => {
    try {
      localStorage.setItem(KEY, id);
    } catch {
      /* приватный режим */
    }
    set({ lotId: id });
  },
}));
