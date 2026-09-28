import { forwardRef, type InputHTMLAttributes, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

const base =
  "w-full rounded-[var(--radius-ctl)] border-[1.5px] border-line bg-surface px-3 py-2 text-[15px] font-semibold text-ink placeholder:font-medium placeholder:text-muted focus:border-accent focus:shadow-[0_0_0_4px_color-mix(in_srgb,var(--accent)_14%,transparent)] focus:outline-none";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(({ className, ...props }, ref) => (
  <input ref={ref} className={cn(base, "h-11", className)} {...props} />
));
Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(({ className, ...props }, ref) => (
  <textarea ref={ref} className={cn(base, "min-h-20", className)} {...props} />
));
Textarea.displayName = "Textarea";

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(({ className, ...props }, ref) => (
  <select ref={ref} className={cn(base, "h-11 pr-8", className)} {...props} />
));
Select.displayName = "Select";

export function Label({ htmlFor, children, className }: { htmlFor: string; children: React.ReactNode; className?: string }) {
  return (
    <label htmlFor={htmlFor} className={cn("mb-1 block text-[11px] font-bold uppercase tracking-[0.06em] text-muted", className)}>
      {children}
    </label>
  );
}

/* Сегментированный переключатель (вкладки-кнопки): направление камеры, драйвер стрелы, период дашборда. */
export function Segmented<T extends string>({ value, onChange, options, ariaLabel, size = "md" }: { value: T; onChange: (v: T) => void; options: { value: T; label: React.ReactNode }[]; ariaLabel: string; size?: "sm" | "md" }) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} className="flex gap-1 rounded-[var(--radius-ctl)] bg-surface-2 p-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          onClick={() => onChange(o.value)}
          className={cn(
            "flex-1 rounded-lg font-bold transition-colors",
            size === "sm" ? "px-3 py-1.5 text-xs" : "px-3 py-2 text-[13px]",
            o.value === value ? "bg-surface text-ink shadow-[0_1px_2px_rgba(16,24,40,0.12)]" : "text-muted hover:text-ink",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
