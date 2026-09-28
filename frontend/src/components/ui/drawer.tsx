import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { Button } from "./button";

/* Боковая панель для форм (новая камера, приглашение сотрудника): не закрывает рабочий экран целиком,
   фокус внутрь, Escape закрывает, фокус возвращается инициатору. */
export function Drawer({ open, onClose, title, children, width = "w-[440px]" }: { open: boolean; onClose: () => void; title: string; children: ReactNode; width?: string }) {
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement;
    panel.current?.querySelector<HTMLElement>("input, textarea, select, button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink/45" onMouseDown={(e) => e.target === e.currentTarget && onClose()} data-testid="drawer">
      <div ref={panel} role="dialog" aria-modal="true" aria-label={title} className={`flex h-full max-w-full flex-col overflow-y-auto bg-surface p-5 shadow-pop ${width}`}>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-lg font-extrabold">{title}</h2>
          <Button variant="ghost" size="icon" className="ml-auto" onClick={onClose} aria-label="Закрыть">
            <X className="h-5 w-5" aria-hidden="true" />
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}
