import { useRef } from "react";
import { loadColor, rowSize, snap, spotsOf, ZONE_COLORS, type LayoutDoc } from "@/lib/layout";
import { cn } from "@/lib/cn";

export type ElementKind = "zone" | "row" | "road" | "device" | "label";
export interface Selection { kind: ElementKind; id: string }
export interface ZoneLoad { occupied: number; capacity: number }
export interface DeviceInfo { name: string; status: string }

const STATUS_COLOR: Record<string, string> = {
  ONLINE: "#16a34a",
  OPEN: "#16a34a",
  CLOSED: "#16a34a",
  DEGRADED: "#d97706",
  SILENT: "#d97706",
  OFFLINE: "#dc2626",
  FAULT: "#dc2626",
  DISABLED: "#94a3b8",
  UNKNOWN: "#94a3b8",
};

/** SVG-схема площадки. mode="view" — живая карта (заливка зон по занятости, статусы устройств);
    mode="edit" — выбор и перетаскивание элементов с привязкой к сетке, угол-ручка меняет размер зон и проездов. */
export function LayoutCanvas({
  doc,
  mode,
  selected,
  onSelect,
  onChange,
  zoneLoads,
  devices,
  className,
}: {
  doc: LayoutDoc;
  mode: "view" | "edit";
  selected?: Selection | null;
  onSelect?: (s: Selection | null) => void;
  onChange?: (s: Selection, patch: { x?: number; y?: number; w?: number; h?: number }) => void;
  zoneLoads?: Record<string, ZoneLoad>;
  devices?: Record<string, DeviceInfo>;
  className?: string;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const drag = useRef<{ sel: Selection; resize: boolean; dx: number; dy: number; ox: number; oy: number } | null>(null);
  const edit = mode === "edit";

  const point = (e: React.PointerEvent): { x: number; y: number } => {
    const el = svg.current;
    const m = el?.getScreenCTM();
    if (!el || !m) return { x: 0, y: 0 };
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  };

  const start = (e: React.PointerEvent, sel: Selection, ox: number, oy: number, resize = false) => {
    if (!edit) return;
    e.stopPropagation();
    onSelect?.(sel);
    const p = point(e);
    drag.current = { sel, resize, dx: p.x - ox, dy: p.y - oy, ox, oy };
    svg.current?.setPointerCapture(e.pointerId);
  };

  const move = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d || !onChange) return;
    const p = point(e);
    const g = doc.grid || 10;
    const clamp = (v: number, max: number) => Math.max(0, Math.min(max, v));
    if (d.resize) {
      onChange(d.sel, { w: Math.max(g * 2, snap(p.x - d.ox, g)), h: Math.max(g * 2, snap(p.y - d.oy, g)) });
    } else {
      onChange(d.sel, { x: clamp(snap(p.x - d.dx, g), doc.width), y: clamp(snap(p.y - d.dy, g), doc.height) });
    }
  };

  const end = (e: React.PointerEvent) => {
    drag.current = null;
    if (svg.current?.hasPointerCapture(e.pointerId)) svg.current.releasePointerCapture(e.pointerId);
  };

  const isSel = (kind: ElementKind, id: string) => selected?.kind === kind && selected.id === id;
  const spots = spotsOf(doc);

  return (
    <svg
      ref={svg}
      viewBox={`0 0 ${doc.width} ${doc.height}`}
      className={cn("block h-auto w-full touch-none select-none rounded-xl bg-surface", edit && "cursor-crosshair", className)}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
      onPointerDown={() => edit && onSelect?.(null)}
      role="img"
      aria-label="lot layout"
      data-testid={edit ? "layout-editor-canvas" : "layout-map"}
    >
      {edit && (
        <>
          <defs>
            <pattern id="grid" width={doc.grid * 5} height={doc.grid * 5} patternUnits="userSpaceOnUse">
              <path d={`M ${doc.grid * 5} 0 L 0 0 0 ${doc.grid * 5}`} fill="none" stroke="#94a3b8" strokeOpacity="0.25" strokeWidth="1" />
            </pattern>
          </defs>
          <rect width={doc.width} height={doc.height} fill="url(#grid)" />
        </>
      )}
      <rect x="0.5" y="0.5" width={doc.width - 1} height={doc.height - 1} fill="none" stroke="#94a3b8" strokeOpacity="0.5" rx="12" />

      {doc.roads.map((r) => (
        <g key={r.id} onPointerDown={(e) => start(e, { kind: "road", id: r.id }, r.x, r.y)} className={cn(edit && "cursor-move")} data-testid="layout-road">
          <rect x={r.x} y={r.y} width={r.w} height={r.h} fill="#64748b" fillOpacity="0.18" stroke={isSel("road", r.id) ? "#2b6cf6" : "none"} strokeWidth="3" rx="4" />
          <line x1={r.x + 8} y1={r.y + r.h / 2} x2={r.x + r.w - 8} y2={r.y + r.h / 2} stroke="#64748b" strokeOpacity="0.5" strokeWidth="3" strokeDasharray="14 12" />
          {r.label && (
            <text x={r.x + r.w / 2} y={r.y + r.h / 2 - 6} textAnchor="middle" fontSize="13" fontWeight="700" className="fill-muted">
              {r.label}
            </text>
          )}
          {edit && isSel("road", r.id) && <Handle x={r.x + r.w} y={r.y + r.h} onDown={(e) => start(e, { kind: "road", id: r.id }, r.x, r.y, true)} />}
        </g>
      ))}

      {doc.zones.map((z) => {
        const load = zoneLoads?.[z.id];
        const ratio = load && load.capacity ? load.occupied / load.capacity : 0;
        const color = load ? loadColor(ratio) : ZONE_COLORS[z.color];
        return (
          <g key={z.id} onPointerDown={(e) => start(e, { kind: "zone", id: z.id }, z.x, z.y)} className={cn(edit && "cursor-move")} data-testid="layout-zone" data-zone={z.id}>
            <rect x={z.x} y={z.y} width={z.w} height={z.h} rx="10" fill={color} fillOpacity={load ? 0.1 + Math.min(1, ratio) * 0.25 : 0.08} stroke={isSel("zone", z.id) ? "#2b6cf6" : color} strokeWidth={isSel("zone", z.id) ? 3 : 2} strokeDasharray={load ? undefined : "8 6"} />
            <text x={z.x + 12} y={z.y + 24} fontSize="16" fontWeight="800" fill={color}>
              {z.name}
              {load && ` · ${load.occupied}/${load.capacity}`}
            </text>
            {edit && isSel("zone", z.id) && <Handle x={z.x + z.w} y={z.y + z.h} onDown={(e) => start(e, { kind: "zone", id: z.id }, z.x, z.y, true)} />}
          </g>
        );
      })}

      {doc.rows.map((r) => {
        const size = rowSize(r);
        return (
          <g key={r.id} onPointerDown={(e) => start(e, { kind: "row", id: r.id }, r.x, r.y)} className={cn(edit && "cursor-move")} data-testid="layout-row">
            <rect x={r.x - 2} y={r.y - 2} width={size.w + 4} height={size.h + 4} fill="transparent" stroke={isSel("row", r.id) ? "#2b6cf6" : "none"} strokeWidth="3" rx="3" />
            {spots
              .filter((s) => s.row === r.id)
              .map((s) => (
                <g key={s.code}>
                  <rect x={s.x + 1} y={s.y + 1} width={s.w - 2} height={s.h - 2} rx="2" fill="#ffffff" fillOpacity="0.55" stroke="#64748b" strokeOpacity="0.7" />
                  {s.w >= 20 && (
                    <text x={s.x + s.w / 2} y={s.y + s.h / 2 + 4} textAnchor="middle" fontSize="9" fontWeight="700" className="fill-ink-2">
                      {s.code}
                    </text>
                  )}
                </g>
              ))}
          </g>
        );
      })}

      {doc.labels.map((l) => (
        <g key={l.id} onPointerDown={(e) => start(e, { kind: "label", id: l.id }, l.x, l.y)} className={cn(edit && "cursor-move")}>
          <text x={l.x} y={l.y} fontSize="15" fontWeight="800" fill={l.kind === "entry" ? "#16a34a" : l.kind === "exit" ? "#e11d48" : "currentColor"} className={l.kind === "text" ? "fill-ink" : undefined} stroke={isSel("label", l.id) ? "#2b6cf6" : "none"} strokeWidth="0.6">
            {l.kind === "entry" ? "▶ " : l.kind === "exit" ? "◀ " : ""}
            {l.text}
          </text>
        </g>
      ))}

      {doc.devices.map((d) => {
        const info = devices?.[d.id];
        const color = info ? (STATUS_COLOR[info.status] ?? "#94a3b8") : "#2b6cf6";
        return (
          <g key={d.id} onPointerDown={(e) => start(e, { kind: "device", id: d.id }, d.x, d.y)} className={cn(edit && "cursor-move")} data-testid="layout-device" data-kind={d.kind} data-status={info?.status}>
            {d.kind === "camera" ? (
              <>
                <circle cx={d.x} cy={d.y} r="13" fill={color} stroke={isSel("device", d.id) ? "#0f172a" : "#fff"} strokeWidth="2.5" />
                <circle cx={d.x} cy={d.y} r="5" fill="#fff" />
              </>
            ) : (
              <>
                <rect x={d.x - 14} y={d.y - 6} width="28" height="12" rx="3" fill={color} stroke={isSel("device", d.id) ? "#0f172a" : "#fff"} strokeWidth="2.5" />
                <line x1={d.x - 8} y1={d.y} x2={d.x + 8} y2={d.y} stroke="#fff" strokeWidth="2" strokeDasharray="4 3" />
              </>
            )}
            {info && (
              <text x={d.x} y={d.y + 28} textAnchor="middle" fontSize="11" fontWeight="700" className="fill-ink-2">
                {info.name}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function Handle({ x, y, onDown }: { x: number; y: number; onDown: (e: React.PointerEvent) => void }) {
  return <rect x={x - 7} y={y - 7} width="14" height="14" rx="3" fill="#2b6cf6" stroke="#fff" strokeWidth="2" className="cursor-nwse-resize" onPointerDown={onDown} data-testid="resize-handle" />;
}
