import { useEffect, useMemo, useState } from "react";
import { Camera, History, LogIn, LogOut, Plus, Save, Sparkles, Trash2, Type } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { ApiError } from "@/api/errors";
import { fetchLayoutVersion, useBarriers, useCameras, useLayoutVersions, useSaveLayout, useUpdateLot, type LayoutOut, type LotOut } from "@/api/hooks";
import { duplicateCodes, generateLayout, newId, nextPrefix, normalize, spotsOf, zoneAt, ZONE_COLOR_KEYS, ZONE_COLORS, type LayoutDoc, type ZoneColor } from "@/lib/layout";
import { LayoutCanvas, type Selection } from "./LayoutCanvas";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label, Segmented, Select } from "@/components/ui/input";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/cn";

type Patch = { x?: number; y?: number; w?: number; h?: number };

/** Редактор схемы площадки: зоны, ряды мест, проезды, подписи, камеры и стрелы; перетаскивание по сетке,
    свойства выбранного элемента справа, версии (открыть старую и сохранить = откат), конфликт правок. */
export function LayoutEditor({ lot, layout, onSaved }: { lot: LotOut; layout: LayoutOut; onSaved?: () => void }) {
  const { t, i18n } = useTranslation();
  const save = useSaveLayout(lot.id);
  const updateLot = useUpdateLot();
  const cameras = useCameras();
  const barriers = useBarriers();
  const versions = useLayoutVersions(lot.id, true);
  const [draft, setDraft] = useState<LayoutDoc>(() => normalize(layout.document));
  const [base, setBase] = useState(layout.version);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [reason, setReason] = useState("");
  const [restored, setRestored] = useState<number | null>(null);

  useEffect(() => {
    if (!dirty) {
      setDraft(normalize(layout.document));
      setBase(layout.version);
    }
  }, [layout, dirty]);

  const lotCameras = (cameras.data ?? []).filter((c) => c.lot_id === lot.id);
  const lotBarriers = (barriers.data ?? []).filter((b) => b.lot_id === lot.id);
  const names = useMemo(() => {
    const m: Record<string, string> = {};
    for (const c of lotCameras) m[`camera:${c.id}`] = c.name;
    for (const b of lotBarriers) m[`barrier:${b.id}`] = b.name;
    return m;
  }, [lotCameras, lotBarriers]);
  const unplaced = [
    ...lotCameras.filter((c) => !draft.devices.some((d) => d.kind === "camera" && d.ref_id === c.id)).map((c) => ({ kind: "camera" as const, id: c.id, name: c.name })),
    ...lotBarriers.filter((b) => !draft.devices.some((d) => d.kind === "barrier" && d.ref_id === b.id)).map((b) => ({ kind: "barrier" as const, id: b.id, name: b.name })),
  ];
  const spots = spotsOf(draft).length;
  const dups = duplicateCodes(draft);

  const edit = (fn: (d: LayoutDoc) => LayoutDoc) => {
    setDraft((d) => fn(structuredClone(d)));
    setDirty(true);
  };

  const onChange = (sel: Selection, p: Patch) =>
    edit((d) => {
      if (sel.kind === "zone") {
        const z = d.zones.find((e) => e.id === sel.id);
        if (!z) return d;
        if (p.x !== undefined && p.y !== undefined) {
          const dx = p.x - z.x;
          const dy = p.y - z.y;
          for (const r of d.rows) if (r.zone_id === z.id) Object.assign(r, { x: r.x + dx, y: r.y + dy });
          for (const dev of d.devices) if (dev.zone_id === z.id) Object.assign(dev, { x: dev.x + dx, y: dev.y + dy });
        }
        Object.assign(z, p);
      } else if (sel.kind === "device") {
        const dev = d.devices.find((e) => e.id === sel.id);
        if (dev) {
          Object.assign(dev, p);
          if (dev.kind === "camera") dev.zone_id = zoneAt(d, dev.x, dev.y)?.id ?? null;
        }
      } else {
        const list: { id: string; x: number; y: number }[] = sel.kind === "row" ? d.rows : sel.kind === "road" ? d.roads : d.labels;
        const el = list.find((e) => e.id === sel.id);
        if (el) Object.assign(el, sel.kind === "label" ? { x: p.x ?? el.x, y: p.y ?? el.y } : p);
        if (sel.kind === "row") {
          const r = d.rows.find((e) => e.id === sel.id);
          const own = r?.zone_id ? d.zones.find((z) => z.id === r.zone_id) : undefined;
          const inside = own && r && r.x >= own.x && r.y >= own.y && r.x <= own.x + own.w && r.y <= own.y + own.h;
          if (r && p.x !== undefined && !inside) r.zone_id = zoneAt(d, r.x + 2, r.y + 2)?.id ?? null;
        }
      }
      return d;
    });

  const add = (kind: "zone" | "row" | "road" | "entry" | "exit" | "text") =>
    edit((d) => {
      const zone = selected?.kind === "zone" ? d.zones.find((z) => z.id === selected.id) : d.zones[0];
      let sel: Selection;
      if (kind === "zone") {
        const id = newId("z", d);
        d.zones.push({ id, name: `${t("map.addZone")} ${d.zones.length + 1}`, color: ZONE_COLOR_KEYS[d.zones.length % ZONE_COLOR_KEYS.length]!, x: 40 + d.zones.length * 30, y: 40 + d.zones.length * 30, w: 400, h: 280 });
        sel = { kind: "zone", id };
      } else if (kind === "row") {
        const id = newId("r", d);
        const x = zone ? zone.x + 20 : 60;
        const y = zone ? zone.y + 40 + d.rows.filter((r) => r.zone_id === zone.id).length * 70 : 60;
        d.rows.push({ id, zone_id: zone?.id ?? null, x, y, count: 10, direction: "h", prefix: nextPrefix(d), start: 1, spot_w: 25, spot_h: 50 });
        sel = { kind: "row", id };
      } else if (kind === "road") {
        const id = newId("road", d);
        d.roads.push({ id, x: 40, y: Math.max(0, d.height - 110), w: 400, h: 50, label: null });
        sel = { kind: "road", id };
      } else {
        const id = newId(kind, d);
        d.labels.push({ id, kind, x: 60, y: Math.max(20, d.height - 30), text: kind === "entry" ? t("map.addEntry") : kind === "exit" ? t("map.addExit") : t("map.addText") });
        sel = { kind: "label", id };
      }
      setSelected(sel);
      return d;
    });

  const place = (kind: "camera" | "barrier", refId: string) =>
    edit((d) => {
      const id = newId(kind === "camera" ? "cam" : "bar", d);
      const x = Math.round(d.width / 2);
      const y = Math.round(d.height / 2);
      d.devices.push({ id, kind, ref_id: refId, x, y, zone_id: kind === "camera" ? (zoneAt(d, x, y)?.id ?? null) : null });
      setSelected({ kind: "device", id });
      return d;
    });

  const remove = () => {
    if (!selected) return;
    edit((d) => {
      if (selected.kind === "zone") {
        d.zones = d.zones.filter((z) => z.id !== selected.id);
        for (const r of d.rows) if (r.zone_id === selected.id) r.zone_id = null;
        for (const dev of d.devices) if (dev.zone_id === selected.id) dev.zone_id = null;
      } else if (selected.kind === "row") d.rows = d.rows.filter((r) => r.id !== selected.id);
      else if (selected.kind === "road") d.roads = d.roads.filter((r) => r.id !== selected.id);
      else if (selected.kind === "device") d.devices = d.devices.filter((r) => r.id !== selected.id);
      else d.labels = d.labels.filter((r) => r.id !== selected.id);
      return d;
    });
    setSelected(null);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if ((e.key === "Delete" || e.key === "Backspace") && selected && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") {
        e.preventDefault();
        remove();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const quick = () => {
    if ((draft.zones.length || draft.rows.length) && !window.confirm(t("map.replace"))) return;
    setDraft(generateLayout({ capacity: lot.capacity, zoneName: lot.name, cameras: lotCameras, barriers: lotBarriers }));
    setDirty(true);
    setSelected(null);
  };

  const submit = () =>
    save.mutate(
      { base_version: base, document: draft as unknown as Record<string, unknown>, reason: reason.trim() || null },
      {
        onSuccess: (saved) => {
          setDirty(false);
          setBase(saved.version);
          setRestored(null);
          setReason("");
          toast.ok(fmt(t("map.saved"), { v: saved.version }));
          onSaved?.();
        },
      },
    );

  const conflict = save.error instanceof ApiError && save.error.code === "LAYOUT_CONFLICT";

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_300px]" data-testid="layout-editor">
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => add("zone")} data-testid="add-zone"><Plus className="h-4 w-4" aria-hidden="true" /> {t("map.addZone")}</Button>
          <Button size="sm" variant="outline" onClick={() => add("row")} data-testid="add-row"><Plus className="h-4 w-4" aria-hidden="true" /> {t("map.addRow")}</Button>
          <Button size="sm" variant="outline" onClick={() => add("road")} data-testid="add-road"><Plus className="h-4 w-4" aria-hidden="true" /> {t("map.addRoad")}</Button>
          <Button size="sm" variant="ghost" onClick={() => add("entry")}><LogIn className="h-4 w-4" aria-hidden="true" /> {t("map.addEntry")}</Button>
          <Button size="sm" variant="ghost" onClick={() => add("exit")}><LogOut className="h-4 w-4" aria-hidden="true" /> {t("map.addExit")}</Button>
          <Button size="sm" variant="ghost" onClick={() => add("text")}><Type className="h-4 w-4" aria-hidden="true" /> {t("map.addText")}</Button>
          <Button size="sm" variant="ghost" className="ml-auto" onClick={quick} data-testid="quick-layout"><Sparkles className="h-4 w-4" aria-hidden="true" /> {t("map.quick")}</Button>
        </div>
        <Card className="p-2">
          <LayoutCanvas doc={draft} mode="edit" selected={selected} onSelect={setSelected} onChange={onChange} />
        </Card>
        <div className="flex flex-wrap items-center gap-3 text-[13px] font-semibold">
          <span data-testid="editor-spots">{t("map.spots")}: <b>{spots}</b></span>
          {spots > 0 && spots !== lot.capacity && (
            <span className="flex flex-wrap items-center gap-2 text-warn-ink" data-testid="capacity-mismatch">
              {fmt(t("map.capacityMismatch"), { spots, capacity: lot.capacity })}
              <Button size="sm" variant="outline" disabled={updateLot.isPending} onClick={() => updateLot.mutate({ lotId: lot.id, body: { capacity: spots, reason: t("map.capacityReason") } }, { onSuccess: () => toast.ok(t("lots.saved")) })} data-testid="set-capacity">
                {fmt(t("map.setCapacity"), { spots })}
              </Button>
            </span>
          )}
          {dups.length > 0 && <span className="text-bad-ink">{fmt(t("map.duplicates"), { codes: dups.slice(0, 6).join(", ") })}</span>}
        </div>
        {updateLot.isError && <ErrorBox error={updateLot.error} />}
      </div>

      <div className="space-y-3">
        <Card className="space-y-2">
          {restored !== null && <p className="rounded-lg bg-accent-soft p-2 text-[12.5px] font-bold text-accent-ink">{fmt(t("map.restoreHint"), { v: restored })}</p>}
          {dirty && <p className="text-[12.5px] font-bold text-warn-ink" data-testid="editor-dirty">{t("map.unsaved")}</p>}
          <Label htmlFor="layout-reason">{t("map.reason")}</Label>
          <Input id="layout-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("map.reasonPh")} />
          {save.isError && !conflict && <ErrorBox error={save.error} />}
          {conflict && (
            <div className="rounded-lg bg-warn-soft p-2 text-[12.5px] font-bold text-warn-ink">
              {t("map.conflict")}{" "}
              <button type="button" className="underline" onClick={() => { setDirty(false); save.reset(); onSaved?.(); }}>
                {t("map.reload")}
              </button>
            </div>
          )}
          <Button className="w-full" disabled={!dirty || dups.length > 0 || save.isPending} onClick={submit} data-testid="save-layout">
            <Save className="h-4 w-4" aria-hidden="true" /> {t("map.save")}
          </Button>
        </Card>

        <Card>
          <Properties draft={draft} selected={selected} names={names} edit={edit} onRemove={remove} />
        </Card>

        <Card>
          <div className="mb-2 flex items-center gap-2 text-[13px] font-extrabold"><Camera className="h-4 w-4" aria-hidden="true" /> {t("map.place")}</div>
          {unplaced.length === 0 ? (
            <p className="text-[12.5px] font-semibold text-muted">{t("map.placed")}</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {unplaced.map((u) => (
                <Button key={`${u.kind}:${u.id}`} size="sm" variant="outline" onClick={() => place(u.kind, u.id)} data-testid={`place-${u.kind}`}>
                  {u.kind === "camera" ? t("map.camera") : t("map.barrier")}: {u.name}
                </Button>
              ))}
            </div>
          )}
        </Card>

        <Card>
          <div className="mb-2 flex items-center gap-2 text-[13px] font-extrabold"><History className="h-4 w-4" aria-hidden="true" /> {t("map.versions")}</div>
          <ul className="max-h-48 space-y-1 overflow-auto text-[12.5px]">
            {(versions.data ?? []).map((v) => (
              <li key={v.version} className={cn("flex items-center gap-2", v.version === base && !restored && "font-bold")}>
                <span>{fmt(t("map.version"), { v: v.version })}</span>
                <span className="text-muted">{new Date(v.created_at).toLocaleString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} · {v.spots}</span>
                {v.version !== base && (
                  <button
                    type="button"
                    className="ml-auto font-bold text-accent-ink underline"
                    onClick={async () => {
                      const old = await fetchLayoutVersion(lot.id, v.version);
                      setDraft(normalize(old.document));
                      setRestored(v.version);
                      setDirty(true);
                      setSelected(null);
                    }}
                  >
                    {t("map.open")}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}

function Properties({ draft, selected, names, edit, onRemove }: { draft: LayoutDoc; selected: Selection | null; names: Record<string, string>; edit: (fn: (d: LayoutDoc) => LayoutDoc) => void; onRemove: () => void }) {
  const { t } = useTranslation();
  const num = (v: string, fallback: number) => (Number.isFinite(Number(v)) && v !== "" ? Math.round(Number(v)) : fallback);
  if (!selected) {
    return (
      <div className="space-y-2">
        <div className="text-[13px] font-extrabold">{t("map.canvas")}</div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <Label htmlFor="c-w">{t("map.width")}</Label>
            <Input id="c-w" type="number" min={300} max={5000} value={draft.width} onChange={(e) => edit((d) => ({ ...d, width: Math.min(5000, Math.max(300, num(e.target.value, d.width))) }))} />
          </div>
          <div>
            <Label htmlFor="c-h">{t("map.height")}</Label>
            <Input id="c-h" type="number" min={200} max={5000} value={draft.height} onChange={(e) => edit((d) => ({ ...d, height: Math.min(5000, Math.max(200, num(e.target.value, d.height))) }))} />
          </div>
        </div>
        <p className="text-[12.5px] font-semibold text-muted">{t("map.select")}</p>
      </div>
    );
  }
  const zoneSelect = (value: string | null | undefined, onChange: (v: string | null) => void, id: string) => (
    <>
      <Label htmlFor={id}>{t("map.zone")}</Label>
      <Select id={id} value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{t("map.noZone")}</option>
        {draft.zones.map((z) => (
          <option key={z.id} value={z.id}>
            {z.name}
          </option>
        ))}
      </Select>
    </>
  );
  const body = (() => {
    if (selected.kind === "zone") {
      const z = draft.zones.find((e) => e.id === selected.id);
      if (!z) return null;
      const set = (patch: Partial<typeof z>) => edit((d) => { Object.assign(d.zones.find((e) => e.id === z.id)!, patch); return d; });
      return (
        <>
          <Label htmlFor="p-name">{t("map.name")}</Label>
          <Input id="p-name" value={z.name} maxLength={30} onChange={(e) => set({ name: e.target.value })} data-testid="prop-name" />
          <div className="mt-2 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("map.color")}</div>
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("map.color")}>
            {ZONE_COLOR_KEYS.map((c: ZoneColor) => (
              <button key={c} type="button" role="radio" aria-checked={z.color === c} aria-label={c} onClick={() => set({ color: c })} className={cn("h-6 w-6 rounded-full border-2", z.color === c ? "border-ink" : "border-transparent")}>
                <svg viewBox="0 0 10 10" className="h-full w-full"><circle cx="5" cy="5" r="5" fill={ZONE_COLORS[c]} /></svg>
              </button>
            ))}
          </div>
          <Label htmlFor="p-cap" className="mt-2">{t("map.capacity")}</Label>
          <Input id="p-cap" type="number" min={0} placeholder={t("map.capacityAuto")} value={z.capacity ?? ""} onChange={(e) => set({ capacity: e.target.value === "" ? null : Math.max(0, num(e.target.value, 0)) })} />
        </>
      );
    }
    if (selected.kind === "row") {
      const r = draft.rows.find((e) => e.id === selected.id);
      if (!r) return null;
      const set = (patch: Partial<typeof r>) => edit((d) => { Object.assign(d.rows.find((e) => e.id === r.id)!, patch); return d; });
      return (
        <div className="grid grid-cols-2 gap-2">
          <div>
            <Label htmlFor="p-count">{t("map.count")}</Label>
            <Input id="p-count" type="number" min={1} max={200} value={r.count} onChange={(e) => set({ count: Math.min(200, Math.max(1, num(e.target.value, r.count))) })} data-testid="prop-count" />
          </div>
          <div>
            <Label htmlFor="p-prefix">{t("map.prefix")}</Label>
            <Input id="p-prefix" maxLength={6} value={r.prefix} onChange={(e) => set({ prefix: e.target.value.toUpperCase().replace(/[^A-Z0-9-]/g, "") })} data-testid="prop-prefix" />
          </div>
          <div>
            <Label htmlFor="p-start">{t("map.start")}</Label>
            <Input id="p-start" type="number" min={0} value={r.start} onChange={(e) => set({ start: Math.max(0, num(e.target.value, r.start)) })} />
          </div>
          <div>
            <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("map.direction")}</div>
            <Segmented<"h" | "v"> ariaLabel={t("map.direction")} size="sm" value={r.direction} onChange={(v) => set({ direction: v })} options={[{ value: "h", label: t("map.horizontal") }, { value: "v", label: t("map.vertical") }]} />
          </div>
          <div className="col-span-2">{zoneSelect(r.zone_id, (v) => set({ zone_id: v }), "p-row-zone")}</div>
          <div className="col-span-2">
            <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("map.spotSize")}</div>
            <div className="flex items-center gap-2">
              <Input aria-label="w" type="number" min={8} max={200} value={r.spot_w} onChange={(e) => set({ spot_w: Math.min(200, Math.max(8, num(e.target.value, r.spot_w))) })} />
              <span>×</span>
              <Input aria-label="h" type="number" min={8} max={200} value={r.spot_h} onChange={(e) => set({ spot_h: Math.min(200, Math.max(8, num(e.target.value, r.spot_h))) })} />
            </div>
          </div>
        </div>
      );
    }
    if (selected.kind === "road") {
      const r = draft.roads.find((e) => e.id === selected.id);
      if (!r) return null;
      return (
        <>
          <Label htmlFor="p-road">{t("map.label")}</Label>
          <Input id="p-road" maxLength={40} value={r.label ?? ""} onChange={(e) => edit((d) => { d.roads.find((x) => x.id === r.id)!.label = e.target.value || null; return d; })} />
        </>
      );
    }
    if (selected.kind === "label") {
      const l = draft.labels.find((e) => e.id === selected.id);
      if (!l) return null;
      return (
        <>
          <Label htmlFor="p-label">{t("map.label")}</Label>
          <Input id="p-label" maxLength={40} value={l.text} onChange={(e) => edit((d) => { d.labels.find((x) => x.id === l.id)!.text = e.target.value || "—"; return d; })} />
        </>
      );
    }
    const dev = draft.devices.find((e) => e.id === selected.id);
    if (!dev) return null;
    return (
      <>
        <div className="text-[13.5px] font-bold">
          {dev.kind === "camera" ? t("map.camera") : t("map.barrier")}: {names[`${dev.kind}:${dev.ref_id}`] ?? dev.ref_id.slice(0, 8)}
        </div>
        {dev.kind === "camera" && (
          <>
            {zoneSelect(dev.zone_id, (v) => edit((d) => { d.devices.find((x) => x.id === dev.id)!.zone_id = v; return d; }), "p-dev-zone")}
            <p className="text-[12px] font-semibold text-muted">{t("map.cameraZoneHint")}</p>
          </>
        )}
      </>
    );
  })();
  return (
    <div className="space-y-1" data-testid="layout-properties">
      {body}
      <Button size="sm" variant="ghost" className="mt-2 text-bad-ink" onClick={onRemove} data-testid="remove-element">
        <Trash2 className="h-4 w-4" aria-hidden="true" /> {t("map.remove")}
      </Button>
    </div>
  );
}
