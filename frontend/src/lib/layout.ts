/** Схема площадки (v2 этап 5) — тот же документ, что проверяет бэкенд (tenancy/domain/layout.py). */

export type ZoneColor = "blue" | "green" | "amber" | "violet" | "rose" | "teal" | "gray";
export interface LZone { id: string; name: string; color: ZoneColor; x: number; y: number; w: number; h: number; capacity?: number | null }
export interface LRow { id: string; zone_id?: string | null; x: number; y: number; count: number; direction: "h" | "v"; prefix: string; start: number; spot_w: number; spot_h: number }
export interface LRoad { id: string; x: number; y: number; w: number; h: number; label?: string | null }
export interface LDevice { id: string; kind: "camera" | "barrier"; ref_id: string; x: number; y: number; zone_id?: string | null }
export interface LLabel { id: string; kind: "entry" | "exit" | "text"; x: number; y: number; text: string }
export interface LayoutDoc { width: number; height: number; grid: number; zones: LZone[]; rows: LRow[]; roads: LRoad[]; devices: LDevice[]; labels: LLabel[] }
export interface Spot { code: string; zone_id: string | null; x: number; y: number; w: number; h: number; row: string }

export const ZONE_COLORS: Record<ZoneColor, string> = {
  blue: "#2b6cf6",
  green: "#16a34a",
  amber: "#d97706",
  violet: "#7c3aed",
  rose: "#e11d48",
  teal: "#0d9488",
  gray: "#64748b",
};
export const ZONE_COLOR_KEYS = Object.keys(ZONE_COLORS) as ZoneColor[];

export function emptyLayout(): LayoutDoc {
  return { width: 1200, height: 800, grid: 10, zones: [], rows: [], roads: [], devices: [], labels: [] };
}

/** Приводит документ с сервера к полному виду (отсутствующие списки — пустые, значения по умолчанию рядов). */
export function normalize(raw: Record<string, unknown> | null | undefined): LayoutDoc {
  const d = { ...emptyLayout(), ...(raw as Partial<LayoutDoc> | undefined) };
  return {
    width: d.width,
    height: d.height,
    grid: d.grid ?? 10,
    zones: (d.zones ?? []).map((z) => ({ ...z, color: z.color ?? "blue" })),
    rows: (d.rows ?? []).map((r) => ({ ...r, direction: r.direction ?? "h", prefix: r.prefix ?? "", start: r.start ?? 1, spot_w: r.spot_w ?? 25, spot_h: r.spot_h ?? 50 })),
    roads: d.roads ?? [],
    devices: d.devices ?? [],
    labels: d.labels ?? [],
  };
}

export function snap(v: number, grid: number): number {
  return Math.round(v / grid) * grid;
}

export function rowSize(r: LRow): { w: number; h: number } {
  return r.direction === "h" ? { w: r.spot_w * r.count, h: r.spot_h } : { w: r.spot_w, h: r.spot_h * r.count };
}

export function spotsOf(doc: LayoutDoc): Spot[] {
  const spots: Spot[] = [];
  for (const r of doc.rows) {
    for (let i = 0; i < r.count; i++) {
      const dx = r.direction === "h" ? r.spot_w * i : 0;
      const dy = r.direction === "h" ? 0 : r.spot_h * i;
      spots.push({ code: `${r.prefix}${r.start + i}`, zone_id: r.zone_id ?? null, x: r.x + dx, y: r.y + dy, w: r.spot_w, h: r.spot_h, row: r.id });
    }
  }
  return spots;
}

/** Повторяющиеся коды мест — сервер отклонит такую схему; редактор подсвечивает заранее. */
export function duplicateCodes(doc: LayoutDoc): string[] {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const s of spotsOf(doc)) (seen.has(s.code) ? dup : seen).add(s.code);
  return [...dup];
}

export function zoneCapacity(doc: LayoutDoc, zone: LZone): number {
  if (zone.capacity != null) return zone.capacity;
  return spotsOf(doc).filter((s) => s.zone_id === zone.id).length;
}

/** Зона под точкой; вложенные зоны (этаж внутри площадки) важнее объемлющих — берём самую маленькую. */
export function zoneAt(doc: LayoutDoc, x: number, y: number): LZone | undefined {
  return doc.zones
    .filter((z) => x >= z.x && x <= z.x + z.w && y >= z.y && y <= z.y + z.h)
    .sort((a, b) => a.w * a.h - b.w * b.h)[0];
}

export function newId(prefix: string, doc: LayoutDoc): string {
  const used = new Set([...doc.zones, ...doc.rows, ...doc.roads, ...doc.devices, ...doc.labels].map((e) => e.id));
  for (let i = 1; ; i++) if (!used.has(`${prefix}${i}`)) return `${prefix}${i}`;
}

/** Первый свободный префикс рядов: A, B, C… — коды мест не пересекаются. */
export function nextPrefix(doc: LayoutDoc): string {
  const used = new Set(doc.rows.map((r) => r.prefix));
  for (const letter of "ABCDEFGHJKLMNPRSTUVWXYZ") if (!used.has(letter)) return letter;
  return `R${doc.rows.length + 1}`;
}

/** Буква ряда: A…Z, затем A2-…Z2- (дефис — чтобы «A2-1» не совпал с «A21» ряда A). */
export function rowPrefix(i: number): string {
  const letter = String.fromCharCode(65 + (i % 26));
  const round = Math.floor(i / 26);
  return round === 0 ? letter : `${letter}${round + 1}-`;
}

/** Быстрая схема для мастера: места рядами по 20 (две стороны проезда), камеры и стрелы у ворот. */
export function generateLayout({ capacity, zoneName, cameras, barriers }: { capacity: number; zoneName: string; cameras: { id: string; direction: string }[]; barriers: { id: string }[] }): LayoutDoc {
  // ширина ряда растёт с площадкой: 60 мест — ряды по 20, 2000 — по 57; холст остаётся в пределах 5000×5000
  const perRow = Math.max(1, Math.min(capacity, Math.min(100, Math.max(20, Math.ceil(Math.sqrt(capacity * 1.6))))));
  const rows = Math.max(1, Math.ceil(capacity / perRow));
  const pairs = Math.ceil(rows / 2);
  const spotW = 25;
  const spotH = 50;
  const width = Math.max(700, 80 + perRow * spotW + 40);
  const pairH = spotH * 2 + 60; // ряд, проезд, ряд
  const height = 140 + pairs * pairH + 60;
  const doc = emptyLayout();
  doc.width = width;
  doc.height = height;
  doc.zones.push({ id: "z1", name: zoneName, color: "blue", x: 40, y: 40, w: width - 80, h: pairs * pairH + 20 });
  let left = capacity;
  for (let i = 0; i < rows; i++) {
    const count = Math.min(perRow, left);
    left -= count;
    const pair = Math.floor(i / 2);
    const y = 60 + pair * pairH + (i % 2 === 0 ? 0 : spotH + 60);
    doc.rows.push({ id: `r${i + 1}`, zone_id: "z1", x: 60, y, count, direction: "h", prefix: rowPrefix(i), start: 1, spot_w: spotW, spot_h: spotH });
    if (i % 2 === 0) doc.roads.push({ id: `road${pair + 1}`, x: 60, y: y + spotH + 5, w: perRow * spotW, h: 50, label: null });
  }
  const gateY = 40 + pairs * pairH + 50;
  doc.roads.push({ id: "gate-road", x: 40, y: gateY, w: width - 80, h: 50, label: "Въезд / выезд" });
  const inCams = cameras.filter((c) => c.direction === "IN");
  const outCams = cameras.filter((c) => c.direction !== "IN");
  inCams.forEach((c, i) => doc.devices.push({ id: `cam-in-${i + 1}`, kind: "camera", ref_id: c.id, x: 80 + i * 40, y: gateY - 30, zone_id: "z1" }));
  outCams.forEach((c, i) => doc.devices.push({ id: `cam-out-${i + 1}`, kind: "camera", ref_id: c.id, x: width - 120 - i * 40, y: gateY - 30 }));
  barriers.forEach((b, i) => doc.devices.push({ id: `bar-${i + 1}`, kind: "barrier", ref_id: b.id, x: 80 + i * 40, y: gateY + 25 }));
  doc.labels.push({ id: "entry", kind: "entry", x: 60, y: gateY + 32, text: "Въезд" });
  doc.labels.push({ id: "exit", kind: "exit", x: width - 140, y: gateY + 32, text: "Выезд" });
  return doc;
}

/** Цвет заполненности: до 70 % — зелёный, до 90 % — жёлтый, дальше — красный. */
export function loadColor(ratio: number): string {
  return ratio >= 0.9 ? "#dc2626" : ratio >= 0.7 ? "#d97706" : "#16a34a";
}
