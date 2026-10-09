import { useState } from "react";
import { useTranslation } from "@/i18n";
import { useLotSessions, useLots, type SessionStatus } from "@/api/hooks";
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
  const sessions = useLotSessions(lotId, filter === "ALL" ? STATUSES : [filter], 100);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("sessions.title")}</h1>
        <div className="ml-auto flex items-center gap-2">
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
        <div>{selected && <SessionPanel sessionId={selected} timeZone={timeZone} onClose={() => setSelected(null)} />}</div>
      </div>
    </div>
  );
}
