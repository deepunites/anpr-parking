import { cn } from "@/lib/cn";

export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return <kbd className={cn("rounded border border-line bg-surface px-1.5 py-0.5 font-sans text-[10.5px] font-bold text-ink-2", className)}>{children}</kbd>;
}
