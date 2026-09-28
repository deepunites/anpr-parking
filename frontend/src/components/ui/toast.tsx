import { create } from "zustand";
import { CheckCircle2, AlertTriangle, XCircle } from "lucide-react";
import { cn } from "@/lib/cn";

/* Тосты подтверждений («Стрела открыта», «Оплата принята»): короткие, снизу справа, исчезают сами. */
type Tone = "ok" | "warn" | "bad";
interface Toast { id: number; tone: Tone; text: string }
interface ToastState { items: Toast[]; push: (tone: Tone, text: string) => void; remove: (id: number) => void }

let seq = 0;
// eslint-disable-next-line react-refresh/only-export-components -- стор рядом с viewport
export const useToasts = create<ToastState>((set) => ({
  items: [],
  push: (tone, text) => {
    const id = ++seq;
    set((s) => ({ items: [...s.items.slice(-3), { id, tone, text }] }));
    setTimeout(() => set((s) => ({ items: s.items.filter((t) => t.id !== id) })), tone === "ok" ? 3500 : 6000);
  },
  remove: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
}));

// eslint-disable-next-line react-refresh/only-export-components -- хелпер рядом со стором
export const toast = { ok: (t: string) => useToasts.getState().push("ok", t), warn: (t: string) => useToasts.getState().push("warn", t), bad: (t: string) => useToasts.getState().push("bad", t) };

export function ToastViewport() {
  const { items, remove } = useToasts();
  if (items.length === 0) return null;
  const icons = { ok: <CheckCircle2 className="h-4 w-4 text-ok" aria-hidden="true" />, warn: <AlertTriangle className="h-4 w-4 text-warn" aria-hidden="true" />, bad: <XCircle className="h-4 w-4 text-bad" aria-hidden="true" /> };
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2" role="status" aria-live="polite" data-testid="toasts">
      {items.map((t) => (
        <button
          key={t.id}
          type="button"
          onClick={() => remove(t.id)}
          className={cn("pointer-events-auto flex items-center gap-2.5 rounded-xl bg-ink px-4 py-3 text-left text-[13.5px] font-bold text-ground shadow-pop")}
        >
          {icons[t.tone]}
          {t.text}
        </button>
      ))}
    </div>
  );
}
