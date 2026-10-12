import { useState } from "react";
import { Eraser } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useCloseStaleSessions, useLotSessions, useLots, useStaleSessions, type SessionStatus } from "@/api/hooks";
import { hasPermission, useAuthStore } from "@/auth/store";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input, Label, Textarea } from "@/components/ui/input";
import { toast } from "@/components/ui/toast";
import { useLotStore } from "@/lib/lot";
import { SessionPanel } from "./SessionPanel";
import { Card } from "@/components/ui/card";
import { Badge, toneFor } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Select } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty";
import { ErrorBox } from "@/components/ErrorBox";
import { formatDuration, formatTime } from "@/lib/format";

const STATUSES: SessionStatus[] = ["ACTIVE", "MANUAL_REVIEW", "CLOSED", "ORPHAN", "CANCELLED"];

export function SessionsPage() {
  const { t, i18n } = useTranslation();
  const { lotId } = useLotStore();
  const lots = useLots();
  const timeZone = lots.data?.find((l) => l.id === lotId)?.timezone;
  const [filter, setFilter] = useState<SessionStatus | "ALL">("ALL");
  const [selected, setSelected] = useState<string | null>(null);
  const [closingStale, setClosingStale] = useState(false);
  const canWrite = hasPermission(useAuthStore((st) => st.me), "sessions:write");
  const sessions = useLotSessions(lotId, filter === "ALL" ? STATUSES : [filter], 100);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("sessions.title")}</h1>
        <div className="ml-auto flex items-center gap-2">
          {canWrite && lotId && (
            <Button variant="outline" size="sm" onClick={() => setClosingStale(true)} data-testid="close-stale">
              <Eraser className="h-4 w-4" aria-hidden="true" /> {t("sessions.closeStale")}
            </Button>
          )}
          <label htmlFor="status-filter" className="text-[11px] font-bold uppercase tracking-[0.06em] text-muted">
            {t("sessions.filter")}
          </label>
          <Select id="status-filter" className="h-9 w-auto" value={filter} onChange={(e) => setFilter(e.target.value as SessionStatus | "ALL")}>
            <option value="ALL">{t("sessions.all")}</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`status.${s}`)}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_460px]">
        <Card>
          {sessions.isPending && <SkeletonRows rows={8} />}
          {sessions.isError && <ErrorBox error={sessions.error} onRetry={() => void sessions.refetch()} />}
          {sessions.data && sessions.data.items.length === 0 && <EmptyState title={t("sessions.empty")} />}
          {sessions.data && sessions.data.items.length > 0 && (
            <Table data-testid="sessions-table">
              <thead>
                <tr>
                  <Th>{t("session.plate")}</Th>
                  <Th>{t("session.status")}</Th>
                  <Th>{t("session.entry")}</Th>
                  <Th>{t("session.exitTime")}</Th>
                  <Th>{t("session.minutes")}</Th>
                </tr>
              </thead>
              <tbody>
                {sessions.data.items.map((s) => (
                  <tr key={s.id} className={`cursor-pointer ${selected === s.id ? "bg-accent-soft" : "hover:bg-surface-2"}`} onClick={() => setSelected(s.id)} data-testid="session-row" data-plate={s.plate ?? ""}>
                    <Td>
                      <button type="button" onClick={() => setSelected(s.id)} className="rounded-md focus-visible:outline-none">
                        <Plate plate={s.plate} size="sm" />
                      </button>
                    </Td>
                    <Td>
                      <Badge tone={toneFor(s.status)}>{t(`status.${s.status}`)}</Badge>
                    </Td>
                    <Td mono>{formatTime(s.entry_time, timeZone, i18n.language)}</Td>
                    <Td mono>{formatTime(s.exit_time, timeZone, i18n.language)}</Td>
                    <Td mono>{formatDuration(s.billing_minutes)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
        <Dialog open={closingStale} onClose={() => setClosingStale(false)} title={t("sessions.closeStale")}>
          {closingStale && lotId && <CloseStaleForm lotId={lotId} timeZone={timeZone} onDone={() => setClosingStale(false)} />}
        </Dialog>
        <div>{selected && <SessionPanel sessionId={selected} timeZone={timeZone} onClose={() => setSelected(null)} />}</div>
      </div>
    </div>
  );
}

/* Машины уезжали незамеченными (не было камеры выезда, камера не работала): незакрытые сессии старше N часов →
   «Осиротевшая» без начисления. Сначала — сколько попадёт (сервер считает, ничего не меняя), закрытие — с причиной. */
function CloseStaleForm({ lotId, timeZone, onDone }: { lotId: string; timeZone?: string; onDone: () => void }) {
  const { t, i18n } = useTranslation();
  const [hours, setHours] = useState("12");
  const [reason, setReason] = useState("");
  const h = Number(hours);
  const valid = Number.isInteger(h) && h >= 1 && h <= 8760;
  const preview = useStaleSessions(lotId, valid ? h : 0);
  const close = useCloseStaleSessions(lotId);
  const found = preview.data?.found ?? 0;
  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        close.mutate(
          { hours: h, reason: reason.trim() },
          {
            onSuccess: (r) => {
              toast.ok(fmt(t("sessions.staleClosed"), { count: r.closed }) + (r.skipped_in_exit ? ` · ${fmt(t("sessions.staleSkipped"), { count: r.skipped_in_exit })}` : ""));
              onDone();
            },
          },
        );
      }}
      data-testid="close-stale-form"
    >
      <p className="rounded-lg bg-surface-2 px-3 py-2 text-[12.5px] font-semibold text-ink-2">{t("sessions.staleHint")}</p>
      <Label htmlFor="stale-hours" className="mt-3">
        {t("sessions.staleHours")}
      </Label>
      <Input id="stale-hours" type="number" min={1} max={8760} className="w-32 font-mono" value={hours} onChange={(e) => setHours(e.target.value)} data-testid="stale-hours" autoFocus />
      <div className="mt-2 rounded-lg bg-warn-soft px-3 py-2 text-[13px] font-bold text-warn-ink" data-testid="stale-found" data-count={preview.data ? found : ""}>
        {!valid || preview.isPending
          ? "…"
          : fmt(t("sessions.staleFound"), { count: found, before: formatTime(preview.data?.older_than ?? null, timeZone, i18n.language) })}
      </div>
      {preview.isError && <ErrorBox error={preview.error} />}
      <Label htmlFor="stale-reason" className="mt-3">
        {t("session.reason")}
      </Label>
      <Textarea id="stale-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("sessions.staleReasonPh")} data-testid="stale-reason" />
      {close.isError && <ErrorBox error={close.error} />}
      <div className="mt-4 flex gap-2">
        <Button type="button" variant="ghost" onClick={onDone}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" variant="danger" className="ml-auto" disabled={!valid || found === 0 || reason.trim().length < 3 || close.isPending} data-testid="stale-submit">
          {close.isPending ? "…" : fmt(t("sessions.staleSubmit"), { count: found })}
        </Button>
      </div>
    </form>
  );
}
