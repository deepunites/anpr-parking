import { useState, type FormEvent } from "react";
import { Search } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useCloseShift, useCurrentShift, useLots, useOpenShift, useSearch, useShifts } from "@/api/hooks";
import { hasPermission, useAuthStore } from "@/auth/store";
import { useLotStore } from "@/lib/lot";
import { SessionPanel } from "./SessionPanel";
import { Card, CardTitle, Eyebrow } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Badge, toneFor } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Money } from "@/components/ui/money";
import { Stat } from "@/components/ui/stat";
import { Table, Td, Th } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/empty";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { formatMinor, formatTime } from "@/lib/format";

/* Касса: смена в цифрах сверху, открытие/закрытие смены с расхождением, поиск машины → карточка с приёмом наличных. */
function ShiftPanel() {
  const { t, i18n } = useTranslation();
  const { lotId } = useLotStore();
  const lots = useLots();
  const timeZone = lots.data?.find((l) => l.id === lotId)?.timezone;
  const shift = useCurrentShift(true);
  const shifts = useShifts(true);
  const open = useOpenShift();
  const close = useCloseShift();
  const [openingFloat, setOpeningFloat] = useState("0");
  const [counted, setCounted] = useState("");
  const [closing, setClosing] = useState(false);

  const submitOpen = (e: FormEvent) => {
    e.preventDefault();
    if (lotId) open.mutate({ lot_id: lotId, opening_float: Math.round((Number(openingFloat) || 0) * 100) }, { onSuccess: () => toast.ok(t("payments.openedToast")) });
  };
  const submitClose = (e: FormEvent) => {
    e.preventDefault();
    if (shift.data)
      close.mutate(
        { shiftId: shift.data.id, countedCash: Math.round((Number(counted) || 0) * 100) },
        {
          onSuccess: (s) => {
            setClosing(false);
            const d = (s as { discrepancy?: number | null }).discrepancy ?? 0;
            if (d === 0) toast.ok(t("payments.closedOk"));
            else toast.warn(`${t("payments.closedDiff")}: ${formatMinor(d, i18n.language)}`);
          },
        },
      );
  };
  const current = shift.data;

  return (
    <div className="space-y-4">
      {shift.isError && <ErrorBox error={shift.error} />}
      {current ? (
        <div className="space-y-4" data-testid="shift-open">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label={t("payments.shift")} value={<span className="text-[18px]">{formatTime(current.opened_at, timeZone, i18n.language)}</span>} sub={t("payments.opened").toLowerCase()} tone="ok" />
            <Stat label={t("payments.openingFloat")} value={<span className="text-[18px]">{formatMinor(current.opening_float, i18n.language)}</span>} />
            <Stat label={t("payments.expected")} value={<span className="text-[18px]">{formatMinor(current.expected_cash ?? current.opening_float, i18n.language)}</span>} sub={t("payments.expectedHint")} />
            <Card className="flex items-center justify-center p-2">
              <Button variant="outline" onClick={() => setClosing((v) => !v)} data-testid="close-shift-toggle">
                {t("payments.closeShift")}
              </Button>
            </Card>
          </div>
          {closing && (
            <Card>
              <CardTitle>{t("payments.closeShift")}</CardTitle>
              <form onSubmit={submitClose} className="flex flex-wrap items-end gap-3">
                <div className="min-w-[220px]">
                  <Label htmlFor="counted">{t("payments.counted")}</Label>
                  <Input id="counted" type="number" min={0} step="0.01" required value={counted} onChange={(e) => setCounted(e.target.value)} className="font-mono text-lg" data-testid="counted-cash" autoFocus />
                </div>
                {counted !== "" && (
                  <div className="text-[13px] font-semibold text-ink-2">
                    {t("payments.discrepancy")}: <Money formatted={formatMinor(Math.round(Number(counted) * 100) - (current.expected_cash ?? current.opening_float), i18n.language)} size="sm" />
                  </div>
                )}
                <Button type="submit" variant="danger" disabled={close.isPending} data-testid="close-shift">
                  {t("payments.closeConfirm")}
                </Button>
                <ErrorBox error={close.error} />
              </form>
            </Card>
          )}
        </div>
      ) : (
        <Card data-testid="shift-closed">
          <CardTitle>{t("payments.shift")}</CardTitle>
          <p className="mb-3 text-[13px] font-semibold text-muted">{t("payments.noShift")}</p>
          <form onSubmit={submitOpen} className="flex flex-wrap items-end gap-3">
            <div className="min-w-[220px]">
              <Label htmlFor="opening-float">{t("payments.openingFloat")}</Label>
              <Input id="opening-float" type="number" min={0} step="0.01" value={openingFloat} onChange={(e) => setOpeningFloat(e.target.value)} className="font-mono text-lg" data-testid="opening-float" />
            </div>
            <Button type="submit" variant="money" disabled={open.isPending || !lotId} data-testid="open-shift">
              {t("payments.openShift")}
            </Button>
            <ErrorBox error={open.error} />
          </form>
        </Card>
      )}
      <Card>
        <CardTitle>{t("payments.history")}</CardTitle>
        {(shifts.data ?? []).length === 0 && <EmptyState title={t("payments.noHistory")} />}
        {(shifts.data ?? []).length > 0 && (
          <Table>
            <thead>
              <tr>
                <Th>{t("payments.opened")}</Th>
                <Th>{t("session.status")}</Th>
                <Th className="text-right">{t("payments.expected")}</Th>
                <Th className="text-right">{t("payments.countedShort")}</Th>
                <Th className="text-right">{t("payments.discrepancy")}</Th>
              </tr>
            </thead>
            <tbody>
              {(shifts.data ?? []).slice(0, 10).map((s) => (
                <tr key={s.id}>
                  <Td mono>{formatTime(s.opened_at, timeZone, i18n.language)}</Td>
                  <Td>
                    <Badge tone={toneFor(s.status)}>{t(`status.${s.status}`, { defaultValue: s.status })}</Badge>
                  </Td>
                  <Td mono className="text-right">{s.status === "CLOSED" ? formatMinor(s.expected_cash, i18n.language) : "—"}</Td>
                  <Td mono className="text-right">{s.status === "CLOSED" ? formatMinor(s.counted_cash, i18n.language) : "—"}</Td>
                  <Td mono className={`text-right ${s.discrepancy ? "text-warn-ink" : ""}`}>{s.status === "CLOSED" ? formatMinor(s.discrepancy, i18n.language) : "—"}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

export function PaymentsPage() {
  const { t } = useTranslation();
  const me = useAuthStore((s) => s.me);
  const { lotId } = useLotStore();
  const lots = useLots();
  const timeZone = lots.data?.find((l) => l.id === lotId)?.timezone;
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const search = useSearch(query);
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-extrabold">{t("payments.title")}</h1>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_460px]">
        <div className="space-y-4">
          {hasPermission(me, "shift:manage") && <ShiftPanel />}
          <Card>
            <Eyebrow className="mb-1">{t("payments.findSession")}</Eyebrow>
            <div className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-accent" aria-hidden="true" />
              <Input value={query} onChange={(e) => setQuery(e.target.value)} className="pl-9 font-mono text-lg uppercase" placeholder={t("live.searchPh")} aria-label={t("payments.findSession")} data-testid="payments-search" />
            </div>
            {search.isError && <ErrorBox error={search.error} />}
            {query.trim().length >= 2 && search.data && search.data.length === 0 && <p className="mt-2 text-[13px] font-semibold text-muted">{t("live.noResults")}</p>}
            <ul className="mt-2 space-y-1">
              {(search.data ?? []).map((s) => (
                <li key={s.id}>
                  <button type="button" className={`flex w-full items-center gap-3 rounded-xl border px-2.5 py-2 text-left ${selected === s.id ? "border-accent/50 bg-accent-soft" : "border-transparent hover:bg-surface-2"}`} onClick={() => setSelected(s.id)} data-testid="payments-session-row" data-plate={s.plate ?? ""}>
                    <Plate plate={s.plate} highlight={query} />
                    <Badge tone={toneFor(s.status)} className="ml-auto">
                      {t(`status.${s.status}`)}
                    </Badge>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        </div>
        <div>{selected ? <SessionPanel sessionId={selected} timeZone={timeZone} onClose={() => setSelected(null)} /> : <Card className="min-h-[16rem]"><EmptyState title={t("live.pickSession")} hint={t("payments.pickHint")} /></Card>}</div>
      </div>
    </div>
  );
}
