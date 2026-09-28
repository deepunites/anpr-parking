import { useState } from "react";
import { cn } from "@/lib/cn";

/* Графики дашборда на чистом SVG (CSP style-src 'self': только SVG-атрибуты и классы-токены). Цвета серий —
   токены --series-1..3 в фиксированном порядке; подписи — цвета текста, не серий; подсказка — SVG-группа. */

export interface Series {
  key: string;
  label: string;
  fill: string; // класс заливки: fill-series-1 …
  dot: string; // класс фона для легенды: bg-series-1 …
}

export interface Column {
  label: string; // подпись оси X
  title: string; // заголовок подсказки
  values: number[]; // по порядку series
}

const PAD = { top: 12, right: 8, bottom: 26, left: 56 };

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

export function Legend({ series }: { series: Series[] }) {
  if (series.length < 2) return null;
  return (
    <ul className="flex flex-wrap gap-3 text-[12.5px] font-bold text-ink-2" aria-label="legend">
      {series.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span className={cn("inline-block h-2.5 w-2.5 rounded-sm", s.dot)} aria-hidden="true" />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

/** Столбцы: stacked=true — сегменты друг на друге (выручка по способам), иначе рядом (въезды/выезды по часам). */
export function BarChart({
  columns,
  series,
  format,
  stacked = true,
  axisEvery = 1,
  testId,
  ariaLabel,
  width: W = 720,
  height: H = 220,
}: {
  columns: Column[];
  series: Series[];
  format: (v: number) => string;
  stacked?: boolean;
  axisEvery?: number;
  testId?: string;
  ariaLabel: string;
  width?: number; // узкая карточка — меньший viewBox, чтобы подписи не мельчали
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const totals = columns.map((c) => (stacked ? c.values.reduce((a, b) => a + b, 0) : Math.max(0, ...c.values)));
  const max = niceMax(Math.max(0, ...totals));
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const slot = innerW / Math.max(columns.length, 1);
  const barW = Math.max(3, Math.min(40, slot * 0.64));
  const y = (v: number) => PAD.top + innerH - (v / max) * innerH;
  const ticks = [0, max / 2, max];
  const tip = hover !== null ? columns[hover] : null;
  const tipX = hover !== null ? Math.min(W - 180, Math.max(PAD.left, PAD.left + slot * hover + slot / 2 - 90)) : 0;
  const tipH = 26 + series.length * 17;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label={ariaLabel} data-testid={testId} onMouseLeave={() => setHover(null)}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} className="stroke-line" strokeWidth="1" />
          <text x={PAD.left - 6} y={y(t) + 4} textAnchor="end" fontSize="11" fontWeight="600" className="fill-muted">
            {format(t)}
          </text>
        </g>
      ))}
      {columns.map((c, i) => {
        const x0 = PAD.left + slot * i + (slot - barW) / 2;
        let acc = 0;
        const parts = c.values.map((v, s) => {
          if (v <= 0) return null;
          if (stacked) {
            const top = y(acc + v);
            const h = Math.max(1, y(acc) - top - (acc > 0 ? 2 : 0)); // 2px зазор между сегментами
            acc += v;
            return <rect key={s} x={x0} y={top} width={barW} height={h} rx={s === c.values.length - 1 || c.values.slice(s + 1).every((n) => n <= 0) ? 3 : 0} className={series[s]!.fill} />;
          }
          const w = (barW - 2 * (series.length - 1)) / series.length;
          const top = y(v);
          return <rect key={s} x={x0 + s * (w + 2)} y={top} width={Math.max(1, w)} height={Math.max(1, PAD.top + innerH - top)} rx="2" className={series[s]!.fill} />;
        });
        return (
          <g key={i}>
            {hover === i && <rect x={PAD.left + slot * i} y={PAD.top} width={slot} height={innerH} className="fill-surface-3" opacity="0.6" />}
            {parts}
            {i % axisEvery === 0 && (
              <text x={PAD.left + slot * i + slot / 2} y={H - 8} textAnchor="middle" fontSize="11" fontWeight="600" className="fill-muted">
                {c.label}
              </text>
            )}
            <rect x={PAD.left + slot * i} y={PAD.top} width={slot} height={innerH + PAD.bottom} fill="transparent" onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} data-testid="chart-hit" />
          </g>
        );
      })}
      <line x1={PAD.left} x2={W - PAD.right} y1={PAD.top + innerH} y2={PAD.top + innerH} className="stroke-muted" strokeWidth="1" />
      {tip && (
        <g pointerEvents="none" data-testid="chart-tooltip">
          <rect x={tipX} y={PAD.top} width="180" height={tipH} rx="8" className="fill-surface stroke-line" strokeWidth="1" />
          <text x={tipX + 10} y={PAD.top + 18} fontSize="12" fontWeight="800" className="fill-ink">
            {tip.title}
          </text>
          {series.map((s, k) => (
            <g key={s.key}>
              <rect x={tipX + 10} y={PAD.top + 28 + k * 17} width="9" height="9" rx="2" className={s.fill} />
              <text x={tipX + 24} y={PAD.top + 36 + k * 17} fontSize="11.5" fontWeight="600" className="fill-ink-2">
                {s.label}: {format(tip.values[k] ?? 0)}
              </text>
            </g>
          ))}
        </g>
      )}
    </svg>
  );
}

/** Горизонтальная полоска доли (способы оплаты, лимиты, доступность). */
export function Meter({ value, max, className, tone = "accent" }: { value: number; max: number; className?: string; tone?: "accent" | "ok" | "warn" | "bad" | "series-1" | "series-2" | "series-3" }) {
  const share = max > 0 ? Math.min(1, value / max) : 0;
  const fills = { accent: "fill-accent", ok: "fill-ok", warn: "fill-warn", bad: "fill-bad", "series-1": "fill-series-1", "series-2": "fill-series-2", "series-3": "fill-series-3" };
  return (
    <svg viewBox="0 0 100 6" preserveAspectRatio="none" className={cn("block h-1.5 w-full", className)} aria-hidden="true">
      <rect width="100" height="6" rx="3" className="fill-surface-3" />
      {share > 0 && <rect width={Math.max(3, share * 100)} height="6" rx="3" className={fills[tone]} />}
    </svg>
  );
}
