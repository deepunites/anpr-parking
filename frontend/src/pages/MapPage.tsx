import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Map as MapIcon, Sparkles } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import { useBarriers, useCameras, useLayout, useLotMap, useLots, useSaveLayout } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { generateLayout, normalize } from "@/lib/layout";
import { useLotSocket } from "@/realtime/useLotSocket";
import { LayoutCanvas, type DeviceInfo, type ZoneLoad } from "@/components/map/LayoutCanvas";
import { LayoutEditor } from "@/components/map/LayoutEditor";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs } from "@/components/ui/tabs";
import { EmptyState } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/cn";

/** Карта площадки (v2 этап 5): живая карта (занятость по зонам, состояние устройств, обновление по WebSocket)
    и редактор схемы для тех, кто настраивает площадки (lots:write). */
export function MapPage() {
  const { t } = useTranslation();
  const { me } = useAuth();
  const canEdit = hasPermission(me, "lots:write");
  const [params, setParams] = useSearchParams();
  const tab = canEdit && params.get("tab") === "editor" ? "editor" : "live";
  const { lotId } = useLotStore();
  const lots = useLots();
  const lot = (lots.data ?? []).find((l) => l.id === lotId) ?? null;
  const layout = useLayout(lotId);

  return (
    <div className="space-y-4" data-testid="map-page">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="flex items-center gap-2 text-2xl font-extrabold">
          <MapIcon className="h-6 w-6 text-accent" aria-hidden="true" /> {t("nav.map")}
        </h1>
        {canEdit && (
          <div className="ml-auto">
            <Tabs
              items={[
                { id: "live", label: t("map.live") },
                { id: "editor", label: t("map.editor") },
              ]}
              value={tab}
              onChange={(id) => setParams(id === "editor" ? { tab: "editor" } : {})}
            />
          </div>
        )}
      </div>
      {layout.isError && <ErrorBox error={layout.error} onRetry={() => void layout.refetch()} />}
      {(layout.isPending || !lot) && !layout.isError && <Skeleton className="h-96 w-full" />}
      {layout.data && lot && tab === "editor" && <LayoutEditor key={lot.id} lot={lot} layout={layout.data} onSaved={() => void layout.refetch()} />}
      {layout.data && lot && tab === "live" && (layout.data.version === 0 ? <NoLayout canEdit={canEdit} lotId={lot.id} capacity={lot.capacity} name={lot.name} onEditor={() => setParams({ tab: "editor" })} /> : <LiveMap lotId={lot.id} />)}
    </div>
  );
}

function NoLayout({ canEdit, lotId, capacity, name, onEditor }: { canEdit: boolean; lotId: string; capacity: number; name: string; onEditor: () => void }) {
  const { t } = useTranslation();
  const quick = useQuickLayout(lotId, capacity, name);
  return (
    <Card>
      <EmptyState
        title={t("map.noLayout")}
        hint={t("map.noLayoutHint")}
        testId="map-empty"
        action={
          canEdit ? (
            <div className="flex flex-wrap justify-center gap-2">
              <Button onClick={() => quick.run()} disabled={quick.pending} data-testid="map-quick">
                <Sparkles className="h-4 w-4" aria-hidden="true" /> {t("map.quick")}
              </Button>
              <Button variant="outline" onClick={onEditor}>
                {t("map.draw")}
              </Button>
            </div>
          ) : undefined
        }
      />
      {quick.error !== null && <ErrorBox error={quick.error} />}
    </Card>
  );
}

/** Быстрая схема: места рядами по вместимости площадки, камеры и стрелы площадки у ворот. Мастер — тот же путь. */
// eslint-disable-next-line react-refresh/only-export-components -- хук рядом с экраном, где он живёт
export function useQuickLayout(lotId: string, capacity: number, name: string) {
  const { t } = useTranslation();
  const cameras = useCameras();
  const barriers = useBarriers();
  const save = useSaveLayout(lotId);
  const layout = useLayout(lotId);
  return {
    pending: save.isPending,
    error: save.error as unknown,
    saved: save.data ?? null,
    run: () => {
      const doc = generateLayout({
        capacity,
        zoneName: name,
        cameras: (cameras.data ?? []).filter((c) => c.lot_id === lotId && c.active),
        barriers: (barriers.data ?? []).filter((b) => b.lot_id === lotId && b.active),
      });
      save.mutate(
        { base_version: layout.data?.version ?? 0, document: doc as unknown as Record<string, unknown>, reason: t("map.quick") },
        { onSuccess: () => toast.ok(t("map.generated")) },
      );
    },
  };
}

function LiveMap({ lotId }: { lotId: string }) {
  const { t } = useTranslation();
  useLotSocket(lotId);
  const map = useLotMap(lotId);
  const doc = useMemo(() => normalize(map.data?.document), [map.data]);
  const loads = useMemo(() => Object.fromEntries((map.data?.zones ?? []).map((z) => [z.id, { occupied: z.occupied, capacity: z.capacity } satisfies ZoneLoad])), [map.data]);
  const devices = useMemo(() => Object.fromEntries((map.data?.devices ?? []).map((d) => [d.element_id, { name: d.name, status: d.status } satisfies DeviceInfo])), [map.data]);
  const [focus, setFocus] = useState<string | null>(null);
  if (map.isPending) return <Skeleton className="h-96 w-full" />;
  if (map.isError) return <ErrorBox error={map.error} onRetry={() => void map.refetch()} />;
  const m = map.data;
  const ratio = m.capacity ? m.occupied / m.capacity : 0;
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_300px]">
      <Card className="p-2">
        <LayoutCanvas doc={doc} mode="view" zoneLoads={loads} devices={devices} selected={focus ? { kind: "zone", id: focus } : null} />
      </Card>
      <div className="space-y-3">
        <Card>
          <div className="text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t("map.occupied")}</div>
          <div className="text-[28px] font-extrabold leading-tight" data-testid="map-total">
            {m.occupied} <span className="text-[16px] text-muted">/ {m.capacity}</span>
          </div>
          <div className={cn("text-[12.5px] font-semibold", ratio >= 0.9 ? "text-bad-ink" : ratio >= 0.7 ? "text-warn-ink" : "text-ok-ink")}>
            <b>{Math.max(0, m.capacity - m.occupied)}</b> {t("map.free")}
          </div>
        </Card>
        <Card>
          <div className="mb-2 text-[13px] font-extrabold">{t("map.zones")}</div>
          <ul className="space-y-2.5">
            {m.zones.map((z) => {
              const r = z.capacity ? z.occupied / z.capacity : 0;
              return (
                <li key={z.id} onMouseEnter={() => setFocus(z.id)} onMouseLeave={() => setFocus(null)} data-testid="map-zone" data-zone={z.id}>
                  <div className="mb-1 flex text-[13px] font-bold">
                    <span>{z.name}</span>
                    <span className="ml-auto font-mono" data-testid="map-zone-count">
                      {z.occupied} / {z.capacity}
                    </span>
                  </div>
                  <progress max={z.capacity || 1} value={Math.min(z.occupied, z.capacity)} aria-label={z.name} className={cn("block h-2 w-full appearance-none overflow-hidden rounded-full bg-surface-2 [&::-webkit-progress-bar]:bg-surface-2", r >= 0.9 ? "[&::-moz-progress-bar]:bg-bad [&::-webkit-progress-value]:bg-bad" : r >= 0.7 ? "[&::-moz-progress-bar]:bg-warn [&::-webkit-progress-value]:bg-warn" : "[&::-moz-progress-bar]:bg-ok [&::-webkit-progress-value]:bg-ok")} />
                </li>
              );
            })}
            <li className="flex text-[13px] font-semibold text-muted" data-testid="map-unzoned">
              <span title={t("map.unzonedHint")}>{t("map.unzoned")}</span>
              <span className="ml-auto font-mono">{m.unzoned_occupied}</span>
            </li>
          </ul>
        </Card>
        {m.devices.length > 0 && (
          <Card>
            <div className="mb-2 text-[13px] font-extrabold">{t("map.devices")}</div>
            <ul className="space-y-1 text-[12.5px] font-semibold">
              {m.devices.map((d) => (
                <li key={d.element_id} className="flex gap-2">
                  <span>{d.kind === "camera" ? t("map.camera") : t("map.barrier")}: {d.name}</span>
                  <span className="ml-auto text-muted">{t(`status.${d.status}`, { defaultValue: d.status })}</span>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </div>
  );
}
