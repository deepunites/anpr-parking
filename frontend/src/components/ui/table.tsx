import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

/* Таблицы справочников: заголовок капителью, строки без рамок-«клеток», моноширинные столбцы через <Td mono>. */
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full border-collapse text-[13.5px] font-semibold", className)} {...props} />
    </div>
  );
}
export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return <th className={cn("border-b border-line px-2.5 py-2 text-left text-[11px] font-bold uppercase tracking-[0.08em] text-muted", className)} {...props} />;
}
export function Td({ className, mono, ...props }: TdHTMLAttributes<HTMLTableCellElement> & { mono?: boolean }) {
  return <td className={cn("border-b border-line-2 px-2.5 py-2.5 align-middle", mono && "font-mono text-[12.5px] text-ink-2", className)} {...props} />;
}
