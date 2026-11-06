import { useDeferredValue, useState } from "react";
import { CarFront, CheckCircle2 } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useCancelGuest, usePartnerCars, usePartnerMe, usePartnerMonth, useValidateGuest, type PartnerCarOut, type ValidationOut } from "@/api/hooks";
import { PlateInput, isPlateValid } from "@/components/vehicle/PlateInput";
import { Card, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Dialog } from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/input";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { formatClock, formatDuration, stayMinutes } from "@/lib/format";

function thisMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** Бармен клуба (роль «Партнёр»): гость называет номер — машина находится на парковке, «Выпустить за счёт клуба» —
    на выезде 0 сум и стрела откроется сама (MediaPark, 2026-11-06). Чужие машины и деньги кассы здесь не видны. */
export function PartnerPage() {
  const { t } = useTranslation();
  const me = usePartnerMe();
  const [plate, setPlate] = useState("");
  const query = useDeferredValue(plate);
  const cars = usePartnerCars(query, isPlateValid(query));
  const validate = useValidateGuest();
  const month = usePartnerMonth(thisMonth());
  const [cancelling, setCancelling] = useState<ValidationOut | null>(null);

  const release = (car: PartnerCarOut) =>
    validate.mutate(car.session_id, {
      onSuccess: () => {
        toast.ok(fmt(t("partner.released"), { plate: car.plate }));
        setPlate("");
      },
    });

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div>
        <h1 className="text-2xl font-extrabold">{t("partner.title")}</h1>
        {me.data && <p className="text-[14px] font-semibold text-muted">{me.data.name}</p>}
        {me.isError && <ErrorBox error={me.error} />}
      </div>

      <Card>
        <PlateInput value={plate} onChange={setPlate} label={t("partner.plate")} autoFocus testId="partner-plate" />
        <p className="mt-2 text-[13px] font-semibold text-muted">{t("partner.hint")}</p>
        {cars.isError && <ErrorBox error={cars.error} />}
        {isPlateValid(query) && cars.data?.length === 0 && (
          <p className="mt-3 rounded-lg bg-warn-soft px-3 py-2 text-[14px] font-semibold text-warn-ink" data-testid="partner-not-found">
            {t("partner.notFound")}
          </p>
        )}
        <ul className="mt-3 space-y-2">
          {cars.data?.map((car) => (
            <li key={car.session_id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line p-3" data-testid="partner-car">
              <CarFront className="h-6 w-6 text-accent" aria-hidden="true" />
              <div className="min-w-0">
                <Plate plate={car.plate} size="md" />
                <div className="mt-1 text-[13px] font-semibold text-muted">
                  {t("partner.entered")} {formatClock(car.entry_time, undefined)} · {formatDuration(stayMinutes(car.entry_time, null))}
                  {!car.exact && <span className="ml-1.5 font-bold text-warn-ink">· {t("partner.similar")}</span>}
                </div>
              </div>
              <div className="ml-auto">
                {car.validated ? (
                  <Badge tone="green">
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" /> {t("partner.already")}
                  </Badge>
                ) : (
                  <Button variant="money" size="lg" disabled={validate.isPending} onClick={() => release(car)} data-testid="partner-release">
                    {t("partner.release")}
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
        <ErrorBox error={validate.error} />
      </Card>

      <Card>
        <CardTitle right={month.data ? <Badge tone="blue">{fmt(t("partner.count"), { n: month.data.count })}</Badge> : undefined}>{t("partner.month")}</CardTitle>
        {month.isError && <ErrorBox error={month.error} />}
        {month.data?.items.length === 0 && <p className="text-[13px] font-semibold text-muted">{t("partner.none")}</p>}
        <ul className="divide-y divide-line-2" data-testid="partner-month">
          {month.data?.items.map((v) => (
            <li key={v.id} className="flex flex-wrap items-center gap-3 py-2">
              <span className="w-24 font-mono text-[13px] font-semibold text-muted">{formatClock(v.created_at, undefined)}</span>
              <Plate plate={v.plate} size="sm" />
              <Badge tone={v.state === "CANCELLED" ? "gray" : v.state === "LEFT" ? "green" : "yellow"}>{t(`partner.state.${v.state}`)}</Badge>
              {v.state === "PARKED" && (
                <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setCancelling(v)} data-testid="partner-cancel">
                  {t("partner.cancel")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      </Card>

      <CancelDialog validation={cancelling} onClose={() => setCancelling(null)} />
    </div>
  );
}

function CancelDialog({ validation, onClose }: { validation: ValidationOut | null; onClose: () => void }) {
  const { t } = useTranslation();
  const cancel = useCancelGuest();
  const [reason, setReason] = useState("");
  return (
    <Dialog open={validation !== null} onClose={onClose} title={t("partner.cancelTitle")}>
      {validation && (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (reason.trim().length >= 3) cancel.mutate({ id: validation.id, reason: reason.trim() }, { onSuccess: onClose });
          }}
        >
          <Plate plate={validation.plate} size="md" />
          <div>
            <Label htmlFor="partner-cancel-reason">{t("session.reason")}</Label>
            <Input id="partner-cancel-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("partner.cancelPh")} />
          </div>
          <ErrorBox error={cancel.error} />
          <Button type="submit" variant="danger" disabled={reason.trim().length < 3 || cancel.isPending}>
            {t("partner.cancelConfirm")}
          </Button>
        </form>
      )}
    </Dialog>
  );
}
