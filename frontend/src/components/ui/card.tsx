import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

/* Карточка — рабочая поверхность; тень только у неё, чтобы рабочие панели отличались от фона. */
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-[var(--radius-card)] border border-line bg-surface p-4 shadow-card", className)} {...props} />;
}

export function CardTitle({ className, children, right, ...props }: HTMLAttributes<HTMLHeadingElement> & { right?: ReactNode }) {
  return (
    <div className="mb-3 flex items-center gap-3">
      <h2 className={cn("text-[15px] font-extrabold", className)} {...props}>
        {children}
      </h2>
      {right !== undefined && <div className="ml-auto flex items-center gap-2">{right}</div>}
    </div>
  );
}

/* Подпись-«бровка» над полем или группой: маленькие капители, как на приборной панели. */
export function Eyebrow({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("text-[11px] font-bold uppercase tracking-[0.08em] text-muted", className)} {...props} />;
}
