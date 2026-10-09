import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeftFromLine, ArrowRightToLine, BadgeDollarSign, DoorOpen, PenLine, Search } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useBarriers, useCameras, useLotSessions, useLots, useOccupancy, useReviewQueue, useSearch, type SessionOut } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { useLotSocket, type FeedItem } from "@/realtime/useLotSocket";
import { ConnectionIndicator } from "@/components/layout/AppShell";
import { SessionPanel } from "./SessionPanel";
import { Card, CardTitle } from "@/components/ui/card";
import { Tabs } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Stat } from "@/components/ui/stat";
import { Kbd } from "@/components/ui/kbd";
import { Input } from "@/components/ui/input";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty";
import { ErrorBox } from "@/components/ErrorBox";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Drawer } from "@/components/ui/drawer";
import { ManualEntryForm } from "@/components/vehicle/ManualEntryForm";
import { vehicleLabel } from "@/components/vehicle/vehicle";
import { hasPermission, useAuthStore } from "@/auth/store";
import { useMediaQuery, WIDE } from "@/lib/useMediaQuery";
import { detectFormat, normalizePlate } from "@/lib/plate";

function feedLabel(item: FeedItem, t: (k: string) => string): string {
  switch (item.type) {
    case "ENTRY":
      return t("live.entry");
    case "EXIT":
      return t("live.exit");
    case "PAYMENT":
      return t("live.payment");
    case "BARRIER":
      return t("live.barrier");
    case "ALERT":
      return t("live.alert");
    case "SESSION_CLOSED":
      return t("live.sessionClosed");
    default:
      return t("live.sessionUpdated");
  }
}

function feedTone(item: FeedItem): "ok" | "warn" | "bad" | "accent" | "muted" {
  if (item.type === "ALERT") return "bad";
  if (item.type === "ENTRY") return "ok";
  if (item.type === "EXIT" || item.type === "SESSION_CLOSED") return "accent";
  if (item.type === "PAYMENT") return String(item.data?.["status"]) === "PAID" ? "ok" : "warn";
  if (item.type === "BARRIER") return String(item.data?.["result"]) === "FAILED" ? "bad" : "muted";
  return "muted";
}

function FeedIcon({ item }: { item: FeedItem }) {
  const tone = feedTone(item);
  const bg = { ok: "bg-ok-soft text-ok-ink", warn: "bg-warn-soft text-warn-ink", bad: "bg-bad-soft text-bad-ink", accent: "bg-accent-soft text-accent-ink", muted: "bg-surface-3 text-ink-2" }[tone];
  const icon =
    item.type === "ENTRY" ? <ArrowRightToLine className="h-3.5 w-3.5" aria-hidden="true" /> : item.type === "EXIT" || item.type === "SESSION_CLOSED" ? <ArrowLeftFromLine className="h-3.5 w-3.5" aria-hidden="true" /> : item.type === "PAYMENT" ? <BadgeDollarSign className="h-3.5 w-3.5" aria-hidden="true" /> : item.type === "BARRIER" ? <DoorOpen className="h-3.5 w-3.5" aria-hidden="true" /> : <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />;
  return <span className={cn("grid h-6 w-6 flex-none place-items-center rounded-md", bg)}>{icon}</span>;
}

/** Время на парковке для строки очереди: ч:мм от въезда до сейчас (тикает раз в минуту). */
function elapsed(entry: string | null | undefined, now: number): string {
  if (!entry) return "—";
  const ms = now - new Date(entry).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "0:00";
  const m = Math.floor(ms / 60000);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

function SessionRow({ s, selected, focused, onSelect, timeZone, now, highlight }: { s: SessionOut; selected: boolean; focused: boolean; onSelect: (id: string) => void; timeZone?: string; now: number; highlight?: string }) {
  const { t, i18n } = useTranslation();
  const review = s.status === "MANUAL_REVIEW";
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(s.id)}
        aria-pressed={selected}
        data-testid="session-row"
        data-plate={s.plate ?? ""}
        data-focused={focused ? "1" : "0"}
        className={cn(
          "flex min-h-12 w-full items-center gap-3 rounded-xl border px-2.5 py-2 text-left transition-colors",
          selected ? "border-accent/50 bg-accent-soft" : review ? "border-warn/50 bg-warn-soft hover:brightness-95" : "border-transparent hover:bg-surface-2",
          focused && !selected && "border-line",
        )}
      >
        <span className="min-w-0">
          <Plate plate={s.plate} highlight={highlight} />
          {(vehicleLabel(t, s.vehicle_make, s.vehicle_model, s.vehicle_color) || s.entry_manual) && (
            <span className="mt-0.5 block truncate text-[11.5px] font-semibold text-muted" data-testid="row-vehicle">
              {vehicleLabel(t, s.vehicle_make, s.vehicle_model, s.vehicle_color)}
              {s.entry_manual && <span className="ml-1 text-warn-ink">· {t("manual.badge")}</span>}
            </span>
          )}
        </span>
        <span className="ml-auto text-right">
          <span className="block text-[11.5px] font-semibold text-muted">
            {review ? t("status.MANUAL_REVIEW") : `${t("live.entry").toLowerCase()} ${formatTime(s.entry_time, timeZone, i18n.language).slice(-8, -3)}`}
          </span>
          <span className={cn("block font-mono text-[15px] font-bold tabular-nums", review && "text-warn-ink")}>{review ? (s.review_reason ?? "") : elapsed(s.entry_time, now)}</span>
        </span>
      </button>
    </li>
  );
}

export function LivePage() {
  const { t } = useTranslation();
  const { lotId } = useLotStore();
  const lots = useLots();
  const lot = lots.data?.find((l) => l.id === lotId);
  const timeZone = lot?.timezone;
  const { status, stale, feed, alerts } = useLotSocket(lotId);
  const occ = useOccupancy(lotId);
  const me = useAuthStore((st) => st.me);
  // кассиру устройства не положены (403) — не запрашиваем и не показываем «0 / 0» с советом добавить камеры
  const seeCameras = hasPermission(me, "cameras:read");
  const seeBarriers = hasPermission(me, "barriers:read");
  const cameras = useCameras(seeCameras);
  const barriers = useBarriers(seeBarriers);
  // на телефоне и планшете карточка машины — поверх очереди, а не третьим блоком внизу страницы
  const wide = useMediaQuery(WIDE);
  const active = useLotSessions(lotId, ["ACTIVE"]);
  const review = useReviewQueue(lotId !== null);
  const [tab, setTab] = useState("active");
  const [selected, setSelected] = useState<string | null>(null);
  const [focusIdx, setFocusIdx] = useState(-1);
  const [query, setQuery] = useState("");
  const search = useSearch(query);
  const canManual = hasPermission(me, "sessions:write");
  const [manual, setManual] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);

  const reviewForLot = useMemo(() => (review.data ?? []).filter((s) => s.lot_id === lotId), [review.data, lotId]);
  const searching = query.trim().length >= 2;
  const list: SessionOut[] = useMemo(
    () => (searching ? (search.data ?? []) : tab === "review" ? reviewForLot : (active.data?.items ?? [])),
    [searching, search.data, tab, reviewForLot, active.data],
  );
  const pending = searching ? search.isPending : tab === "review" ? review.isPending : active.isPending;
  const error = searching ? search.error : tab === "review" ? review.error : active.error;

  const camsForLot = (cameras.data ?? []).filter((c) => c.lot_id === lotId && c.active);
  const camsOnline = camsForLot.filter((c) => c.status === "ONLINE").length;
  const barriersForLot = (barriers.data ?? []).filter((b) => b.lot_id === lotId && b.active);
  const barriersBad = barriersForLot.filter((b) => b.state === "ERROR").length;
  const attention = useMemo(() => {
    const items: { key: string; text: string; sessionId?: string; tone: "warn" | "bad" }[] = [];
    for (const s of reviewForLot) items.push({ key: `r-${s.id}`, text: `${t("status.MANUAL_REVIEW")}: ${s.review_reason ?? ""}`, sessionId: s.id, tone: "warn" });
    for (const c of camsForLot) if (c.status === "OFFLINE" || c.status === "SILENT") items.push({ key: `c-${c.id}`, text: `${t("devices.camera")} ${c.code}: ${t(`status.${c.status}`)}`, tone: "bad" });
    for (const a of alerts.slice(0, 5)) items.push({ key: `a-${a.event_id ?? a.receivedAt}`, text: String(a.data?.["message"] ?? a.data?.["kind"] ?? t("live.alert")), tone: "bad" });
    return items;
  }, [reviewForLot, camsForLot, alerts, t]);

  // Клавиатура: / — в поиск, ↑↓ — по очереди, Enter — открыть карточку, Esc — закрыть карточку / очистить поиск
  const onKey = useCallback(
    (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (e.key === "Escape") {
        if (target === searchRef.current && query) setQuery("");
        else if (selected) setSelected(null);
        return;
      }
      if (typing && target !== searchRef.current) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (list.length === 0) return;
        e.preventDefault();
        setFocusIdx((i) => (e.key === "ArrowDown" ? Math.min(i + 1, list.length - 1) : Math.max(i - 1, 0)));
      }
      if (e.key === "Enter" && focusIdx >= 0 && list[focusIdx]) {
        e.preventDefault();
        setSelected(list[focusIdx]!.id);
      }
    },
    [list, focusIdx, selected, query],
  );
  useEffect(() => {
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onKey]);
  useEffect(() => setFocusIdx(-1), [tab, searching]);

  const occupied = occ.data?.occupied ?? 0;
  const capacity = occ.data?.capacity ?? lot?.capacity ?? 0;
  const ratio = capacity > 0 ? occupied / capacity : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.live")}</h1>
        <ConnectionIndicator status={status} stale={stale} />
        {canManual && lotId && (
          <Button variant="outline" className="ml-auto" onClick={() => setManual(true)} data-testid="manual-entry">
            <PenLine className="h-4 w-4" aria-hidden="true" /> {t("manual.button")}
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Stat
          label={t("occupancy.occupied")}
          value={
            <>
              {occupied} <span className="text-base text-muted">/ {capacity}</span>
            </>
          }
          sub={occ.data ? `${Math.round(ratio * 100)} %` : undefined}
          tone={ratio >= 0.95 ? "warn" : "ink"}
          testId="stat-occupancy"
        />
        <Stat label={t("live.review")} value={reviewForLot.length} tone={reviewForLot.length > 0 ? "warn" : "ink"} testId="stat-review" />
        {seeCameras && (
          <Stat
            label={t("live.cameras")}
            value={
              <>
                {camsOnline} <span className="text-base text-muted">/ {camsForLot.length}</span>
              </>
            }
            sub={camsForLot.length > 0 && camsOnline === camsForLot.length ? "online" : undefined}
            tone={camsForLot.length > 0 && camsOnline < camsForLot.length ? "warn" : "ink"}
            testId="stat-cameras"
          />
        )}
        {seeBarriers && (
          <Stat label={t("live.barriers")} value={barriersForLot.length === 0 ? "—" : barriersBad === 0 ? "OK" : barriersBad} sub={barriersForLot.length > 0 ? `${barriersForLot.length}` : undefined} tone={barriersBad > 0 ? "warn" : "ink"} testId="stat-barriers" />
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[330px_minmax(0,1fr)_300px]">
        {/* Очередь */}
        <Card className="flex min-h-[28rem] flex-col">
          <CardTitle>{t("live.queue")}</CardTitle>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-accent" aria-hidden="true" />
            <Input
              id="plate-search"
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="pl-9 pr-9 font-mono text-lg uppercase"
              placeholder={t("live.searchPh")}
              aria-label={t("live.search")}
              data-testid="plate-search"
            />
            <Kbd className="absolute top-1/2 right-3 hidden -translate-y-1/2 pointer-fine:block">/</Kbd>
          </div>
          <div className="mt-2">
            <Tabs
              items={[
                { id: "active", label: t("live.active"), count: active.data?.items.length ?? 0 },
                { id: "review", label: t("live.review"), count: reviewForLot.length },
              ]}
              value={tab}
              onChange={setTab}
            />
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="mt-2 flex-1 overflow-y-auto">
            {pending && <SkeletonRows />}
            {error && <ErrorBox error={error} />}
            {!pending && !error && list.length === 0 && (searching ? <p className="p-2 text-[13px] font-semibold text-muted">{t("live.noResults")}</p> : <EmptyState title={t("sessions.empty")} hint={camsForLot.length > 0 || !seeCameras ? t("live.emptyHint") : t("live.emptyNoCameras")} testId="queue-empty" />)}
            <ul className="space-y-1" data-testid={searching ? "list-search" : `list-${tab}`}>
              {list.map((s, i) => (
                <SessionRow key={s.id} s={s} selected={selected === s.id} focused={focusIdx === i} onSelect={setSelected} timeZone={timeZone} now={now} highlight={searching ? query : undefined} />
              ))}
            </ul>
          </div>
          {/* подсказки клавиатуры — только где есть мышь и клавиатура, на сенсорном экране они лишние */}
          <div className="mt-3 hidden flex-wrap gap-x-3 gap-y-1 border-t border-line-2 pt-2 text-[11.5px] font-semibold text-muted pointer-fine:flex">
            <span>
              <Kbd>↑</Kbd> <Kbd>↓</Kbd> {t("live.keysQueue")}
            </span>
            <span>
              <Kbd>Enter</Kbd> {t("live.keysOpen")}
            </span>
            <span>
              <Kbd>Ctrl</Kbd>+<Kbd>Enter</Kbd> {t("live.keysCash")}
            </span>
            <span>
              <Kbd>Esc</Kbd> {t("live.keysClose")}
            </span>
          </div>
        </Card>

        {/* Карточка сессии: на широком экране — средняя колонка, на телефоне и планшете — поверх экрана */}
        {wide ? (
          <div className="min-h-[28rem]">
            {selected ? (
              <SessionPanel sessionId={selected} timeZone={timeZone} onClose={() => setSelected(null)} />
            ) : (
              <Card className="flex h-full min-h-[28rem] items-center justify-center">
                <EmptyState title={t("live.pickSession")} hint={t("live.pickHint")} testId="session-empty" />
              </Card>
            )}
          </div>
        ) : (
          selected && (
            <div className="fixed inset-0 z-40 overflow-y-auto bg-ground p-3" role="dialog" aria-modal="true" aria-label={t("live.pickSession")} data-testid="session-sheet">
              <SessionPanel sessionId={selected} timeZone={timeZone} onClose={() => setSelected(null)} />
            </div>
          )
        )}

        {/* Внимание + лента */}
        <Card className="flex min-h-[28rem] flex-col">
          <CardTitle right={attention.length > 0 ? <Badge tone="yellow">{attention.length}</Badge> : undefined}>{t("live.attention")}</CardTitle>
          {attention.length === 0 ? (
            <p className="mb-3 text-[12.5px] font-semibold text-muted">{t("live.attentionEmpty")}</p>
          ) : (
            <ul className="mb-3 space-y-1.5" data-testid="attention">
              {attention.map((a) => (
                <li key={a.key} className={cn("flex items-center gap-2 rounded-lg px-2.5 py-2 text-[13px] font-semibold", a.tone === "warn" ? "bg-warn-soft text-warn-ink" : "bg-bad-soft text-bad-ink")}>
                  <AlertTriangle className="h-4 w-4 flex-none" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate">{a.text}</span>
                  {a.sessionId && (
                    <button type="button" className="rounded-md bg-warn px-2 py-1 text-[11px] font-bold text-[#1b1400]" onClick={() => setSelected(a.sessionId!)}>
                      {t("live.open")}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <CardTitle>{t("live.feed")}</CardTitle>
          {feed.length === 0 && <p className="text-[12.5px] font-semibold text-muted">{t("live.empty")}</p>}
          <ul className="max-h-[32rem] flex-1 space-y-0.5 overflow-auto" aria-live="polite" data-testid="feed">
            {feed.map((item) => {
              const sid = typeof item.data?.["session_id"] === "string" ? item.data["session_id"] : null;
              const plate = typeof item.data?.["plate"] === "string" ? item.data["plate"] : null;
              return (
                <li key={item.event_id ?? item.receivedAt} className="flex items-center gap-2 border-b border-line-2 px-1 py-1.5 text-[13px] font-semibold text-ink-2" data-type={item.type}>
                  <FeedIcon item={item} />
                  <span className="min-w-0 flex-1 truncate">
                    {feedLabel(item, t)}
                    {plate && <Plate plate={plate} size="sm" className="ml-1.5" />}
                    <span className="ml-1.5 text-muted">{String(item.data?.["status"] ?? item.data?.["result"] ?? item.data?.["kind"] ?? "")}</span>
                  </span>
                  {sid && (
                    <button type="button" className="font-mono text-[11px] text-accent-ink underline" onClick={() => setSelected(sid)}>
                      {sid.slice(0, 8)}
                    </button>
                  )}
                  <time className="font-mono text-[11px] text-muted">{new Date(item.receivedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
                </li>
              );
            })}
          </ul>
        </Card>
      </div>
      <Drawer open={manual} onClose={() => setManual(false)} title={t("manual.title")} width="w-[500px]">
        {lotId && (
          <ManualEntryForm
            lotId={lotId}
            initialPlate={detectFormat(normalizePlate(query)) ? normalizePlate(query) : ""}
            onDone={(s) => {
              setManual(false);
              setSelected(s.id);
            }}
            onOpenSession={(id) => {
              setManual(false);
              setSelected(id);
            }}
          />
        )}
      </Drawer>
    </div>
  );
}
