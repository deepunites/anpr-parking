import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/* KPI-плитка: подпись капителью, число моноширинным, справа — маленький спарклайн (SVG на токенах). */
export function Stat({ label, value, sub, tone = "ink", spark, className, testId }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "ink" | "warn" | "ok"; spark?: number[]; className?: string; testId?: string }) {
  const tones = { ink: "", warn: "border-warn/60 bg-[linear-gradient(90deg,var(--warn-soft),var(--surface))]", ok: "" }[tone];
  const valueTone = { ink: "text-ink", warn: "text-warn-ink", ok: "text-ok-ink" }[tone];
  return (
    <div className={cn("relative overflow-hidden rounded-[14px] border border-line bg-surface px-4 py-3 shadow-card", tones, className)} data-testid={testId}>
      <div className="text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">{label}</div>
      <div className={cn("mt-1 font-mono text-[26px] font-extrabold leading-tight tabular-nums", valueTone)}>
        {value}
        {sub !== undefined && <span className="ml-2 font-sans text-xs font-bold text-muted">{sub}</span>}
      </div>
      {spark && spark.length > 1 && <Sparkline data={spark} className="absolute right-3 bottom-2 h-8 w-28" />}
    </div>
  );
}

export function Sparkline({ data, className }: { data: number[]; className?: string }) {
  const w = 120;
  const h = 36;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - 4 - ((v - min) / (max - min || 1)) * (h - 8)] as const);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={className} aria-hidden="true">
      <polygon points={`0,${h} ${line} ${w},${h}`} fill="var(--accent)" opacity="0.15" />
      <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  );
}
