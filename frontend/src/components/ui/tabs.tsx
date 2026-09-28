import { cn } from "@/lib/cn";

export interface TabItem {
  id: string;
  label: string;
  count?: number;
}

/* Вкладки с ARIA (role=tablist) и клавиатурной навигацией стрелками. */
export function Tabs({ items, value, onChange }: { items: TabItem[]; value: string; onChange: (id: string) => void }) {
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const index = items.findIndex((i) => i.id === value);
    if (e.key === "ArrowRight") onChange(items[(index + 1) % items.length]!.id);
    if (e.key === "ArrowLeft") onChange(items[(index - 1 + items.length) % items.length]!.id);
  };
  return (
    <div role="tablist" aria-orientation="horizontal" className="flex gap-1 rounded-[var(--radius-ctl)] bg-surface-2 p-1" onKeyDown={onKey}>
      {items.map((item) => {
        const active = item.id === value;
        return (
          <button
            key={item.id}
            role="tab"
            type="button"
            id={`tab-${item.id}`}
            aria-selected={active}
            aria-controls={`panel-${item.id}`}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(item.id)}
            className={cn(
              "flex min-h-10 flex-1 items-center justify-center gap-2 rounded-lg px-3 text-[13px] font-bold transition-colors",
              active ? "bg-surface text-ink shadow-[0_1px_2px_rgba(16,24,40,0.12)]" : "text-muted hover:text-ink",
            )}
          >
            {item.label}
            {item.count !== undefined && (
              <span className={cn("rounded-md px-1.5 py-0.5 font-mono text-[11px] tabular-nums", item.count > 0 ? "bg-warn text-[#1b1400]" : "bg-surface-3 text-ink-2")}>
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
