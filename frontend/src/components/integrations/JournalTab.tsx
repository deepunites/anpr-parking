import { useState } from "react";
import { X } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useJournal, useLots } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { formatTime } from "@/lib/format";
import { Card, CardTitle } from "@/components/ui/card";
import { Input, Segmented } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";

type OutcomeFilter = "all" | "REJECTED" | "ACCEPTED" | "ERROR";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TONE = { ACCEPTED: "green", DUPLICATE: "gray", QUEUED: "blue", REJECTED: "red", ERROR: "red" } as const;

/* Журнал событий камер за 7 дней: что пришло, что ответили и почему отклонили. Номер — маской. */
export function JournalTab() {
  const { t, i18n } = useTranslation();
  const { lotId } = useLotStore();
  const lots = useLots();
  const timeZone = (lots.data ?? []).find((l) => l.id === lotId)?.timezone;
  const [outcome, setOutcome] = useState<OutcomeFilter>("all");
  const [corr, setCorr] = useState("");
  const correlationId = UUID_RE.test(corr.trim()) ? corr.trim() : null;
  const journal = useJournal({ outcome: outcome === "all" ? null : outcome, correlationId, lotId: correlationId ? null : lotId });
  return (
    <Card>
      <CardTitle
        right={
          <Segmented
            size="sm"
            ariaLabel={t("integrations.journal.filter")}
            value={outcome}
            onChange={setOutcome}
            options={(["all", "REJECTED", "ACCEPTED", "ERROR"] as const).map((v) => ({ value: v, label: t(`integrations.journal.${v}`) }))}
          />
        }
      >
        {t("integrations.journal.title")}
      </CardTitle>
      <p className="mb-3 text-[13px] font-semibold text-ink-2">{t("integrations.journal.hint")}</p>
      <div className="mb-3 flex max-w-xl items-center gap-2">
        <Input value={corr} onChange={(e) => setCorr(e.target.value)} placeholder={t("integrations.journal.corrPh")} aria-label={t("integrations.journal.corr")} className="font-mono text-[13px]" data-testid="journal-corr" />
        {corr && (
          <Button size="icon" variant="ghost" aria-label={t("integrations.cancel")} onClick={() => setCorr("")}>
            <X className="h-4 w-4" aria-hidden="true" />
          </Button>
        )}
      </div>
      {journal.error ? <ErrorBox error={journal.error} onRetry={() => void journal.refetch()} /> : null}
      {journal.isLoading ? (
        <SkeletonRows rows={4} />
      ) : (journal.data ?? []).length === 0 ? (
        <EmptyState title={t("integrations.journal.empty")} hint={t("integrations.journal.emptyHint")} testId="journal-empty" />
      ) : (
        <div className="overflow-x-auto">
          <Table data-testid="journal">
            <thead>
              <tr>
                <Th>{t("integrations.time")}</Th>
                <Th>{t("integrations.journal.source")}</Th>
                <Th>{t("integrations.journal.outcome")}</Th>
                <Th>{t("integrations.journal.plate")}</Th>
                <Th>{t("integrations.journal.reason")}</Th>
                <Th>{t("integrations.journal.latency")}</Th>
                <Th>correlation_id</Th>
              </tr>
            </thead>
            <tbody>
              {(journal.data ?? []).map((r, i) => (
                <tr key={`${r.received_at}-${i}`} data-testid="journal-row" data-outcome={r.outcome}>
                  <Td>{formatTime(r.received_at, timeZone, i18n.language)}</Td>
                  <Td>
                    {r.source}
                    {r.event_type && <span className="text-muted"> · {r.event_type}</span>}
                  </Td>
                  <Td>
                    <Badge tone={TONE[r.outcome as keyof typeof TONE] ?? "gray"}>{t(`integrations.journal.${r.outcome}`, { defaultValue: r.outcome })}</Badge>
                  </Td>
                  <Td mono>{r.plate_masked ?? "—"}</Td>
                  <Td className="max-w-[340px]">
                    {r.status && <span className="font-mono text-[12px] font-bold">{r.status}</span>}
                    {r.http_status && <span className="text-muted"> · HTTP {r.http_status}</span>}
                    {r.message && <div className="truncate text-[12.5px] text-ink-2" title={r.message}>{t(`integrations.reasonText.${r.status}`, { defaultValue: r.message })}</div>}
                  </Td>
                  <Td>{r.latency_ms !== null ? `${r.latency_ms} мс` : "—"}</Td>
                  <Td>
                    {r.correlation_id ? (
                      <button type="button" className="font-mono text-[11.5px] font-bold text-accent" onClick={() => setCorr(r.correlation_id ?? "")} title={r.correlation_id}>
                        {r.correlation_id.slice(0, 8)}
                      </button>
                    ) : (
                      "—"
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}
