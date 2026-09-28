import { cn } from "@/lib/cn";

/* Состояние передаётся цветом И текстом (доступность): бейдж всегда содержит подпись. */
const TONES = {
  green: "bg-ok-soft text-ok-ink",
  yellow: "bg-warn-soft text-warn-ink",
  red: "bg-bad-soft text-bad-ink",
  gray: "bg-surface-3 text-ink-2",
  blue: "bg-accent-soft text-accent-ink",
} as const;

// eslint-disable-next-line react-refresh/only-export-components -- маппинг статусов живёт рядом с бейджем намеренно
export function toneFor(status: string): keyof typeof TONES {
  switch (status) {
    case "ACTIVE":
    case "PAID":
    case "ONLINE":
    case "OK":
    case "OPEN":
      return "green";
    case "MANUAL_REVIEW":
    case "PENDING":
    case "DEGRADED":
    case "PENDING_VERIFICATION":
    case "SILENT":
      return "yellow";
    case "ORPHAN":
    case "FAILED":
    case "OFFLINE":
    case "CANCELLED":
      return "red";
    case "CLOSED":
    case "REFUNDED":
      return "blue";
    default:
      return "gray";
  }
}

export function Badge({ tone = "gray", children, className, ...rest }: { tone?: keyof typeof TONES; children: React.ReactNode; className?: string; title?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.06em]", TONES[tone], className)} {...rest}>
      {children}
    </span>
  );
}

/* Точка состояния для таблиц: цвет + текст рядом. */
export function StatusDot({ tone, children, className }: { tone: keyof typeof TONES; children: React.ReactNode; className?: string }) {
  const color = { green: "bg-ok", yellow: "bg-warn", red: "bg-bad", gray: "bg-muted", blue: "bg-accent" }[tone];
  return (
    <span className={cn("inline-flex items-center gap-2 text-[12.5px] font-bold", className)}>
      <i aria-hidden="true" className={cn("h-2 w-2 rounded-full", color)} />
      {children}
    </span>
  );
}
