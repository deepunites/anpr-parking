import { cn } from "@/lib/cn";

/* Суммы UI не считает и не форматирует: строка приходит с бэкенда (`amount.formatted`). Здесь — только вид. */
export function Money({ formatted, size = "md", tone = "ink", className }: { formatted: string | null | undefined; size?: "sm" | "md" | "lg" | "xl"; tone?: "ink" | "ok" | "muted"; className?: string }) {
  const sizes = { sm: "text-[14px]", md: "text-[18px]", lg: "text-[26px]", xl: "text-[34px]" }[size];
  const tones = { ink: "text-ink", ok: "text-ok-ink", muted: "text-muted" }[tone];
  return (
    <span className={cn("font-mono font-bold tabular-nums leading-none", sizes, tones, className)} data-testid="money">
      {formatted ?? "—"}
    </span>
  );
}
