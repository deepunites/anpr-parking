import { useEffect, useRef, type ReactNode } from "react";

/**
 * Доступный диалог без сторонних библиотек и инлайн-стилей (CSP style-src 'self'): role=dialog, aria-modal,
 * фокус внутрь при открытии, Escape закрывает, фокус возвращается инициатору.
 */
export function Dialog({ open, onClose, title, children, initialFocus }: { open: boolean; onClose: () => void; title: string; children: ReactNode; initialFocus?: React.RefObject<HTMLElement | null> }) {
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement;
    const target = initialFocus?.current ?? panel.current?.querySelector<HTMLElement>("input, textarea, button");
    target?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && panel.current) {
        const focusables = panel.current.querySelectorAll<HTMLElement>("input, textarea, button, [href], select, [tabindex]:not([tabindex='-1'])");
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (!first || !last) return;
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      (opener.current as HTMLElement | null)?.focus?.();
    };
  }, [open, onClose, initialFocus]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 p-4" onClick={onClose} role="presentation">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        className="w-full max-w-lg rounded-[18px] border border-line bg-surface p-6 shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="dialog-title" className="mb-4 text-xl font-extrabold">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
