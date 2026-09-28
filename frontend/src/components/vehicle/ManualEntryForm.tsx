import { useState } from "react";
import { AlertTriangle, Car } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useManualEntry, useVehicleLookup, type SessionOut } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { PlateInput, isPlateValid } from "./PlateInput";
import { VehicleFields } from "./VehicleFields";
import { EMPTY_VEHICLE, vehicleLabel, type VehicleValue } from "./vehicle";
import { cn } from "@/lib/cn";

const AGO = [0, 10, 30, 60];
const REASONS = ["notRecognized", "dirtyPlate", "cameraDown", "guardLetIn"] as const;

function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Ручной въезд: камера не распознала машину — оператор вводит номер (и марку, если видит), время въезда и причину.
    Сессия создаётся как обычная (стоянка считается по тарифу), шлагбаум не открывается — машина уже внутри. */
export function ManualEntryForm({ lotId, initialPlate, onDone, onOpenSession }: { lotId: string; initialPlate?: string; onDone: (s: SessionOut) => void; onOpenSession: (id: string) => void }) {
  const { t } = useTranslation();
  const create = useManualEntry();
  const [plate, setPlate] = useState(initialPlate ?? "");
  const [vehicle, setVehicle] = useState<VehicleValue>(EMPTY_VEHICLE);
  const [ago, setAgo] = useState<number | "custom">(0);
  const [custom, setCustom] = useState(() => toLocalInput(new Date()));
  const [reasonCode, setReasonCode] = useState<(typeof REASONS)[number] | null>("notRecognized");
  const [reasonText, setReasonText] = useState("");
  const valid = isPlateValid(plate);
  const lookup = useVehicleLookup(plate, valid);
  const known = lookup.data;
  const alreadyInside = known?.active_session_id ?? null;
  const reason = [reasonCode ? t(`manual.reasons.${reasonCode}`) : "", reasonText.trim()].filter(Boolean).join(": ");
  const entryTime = ago === "custom" ? new Date(custom) : new Date(Date.now() - ago * 60_000);
  const canSubmit = valid && reason.length >= 3 && !alreadyInside && !create.isPending && !Number.isNaN(entryTime.getTime());

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canSubmit) return;
        create.mutate(
          {
            lotId,
            body: {
              plate,
              entry_time: ago === 0 ? null : entryTime.toISOString(),
              make: vehicle.make.trim() || null,
              model: vehicle.model.trim() || null,
              color: vehicle.color || null,
              reason,
            },
          },
          {
            onSuccess: (s) => {
              toast.ok(t("manual.done"));
              onDone(s);
            },
          },
        );
      }}
      data-testid="manual-entry-form"
    >
      <p className="rounded-xl bg-surface-2 p-3 text-[12.5px] font-semibold text-ink-2">{t("manual.hint")}</p>
      <div>
        <PlateInput value={plate} onChange={setPlate} label={t("session.plate")} autoFocus testId="manual-plate" />
        {known?.known && !alreadyInside && vehicleLabel(t, known.make, known.model, known.color) && (
          <p className="mt-1 flex items-center gap-1.5 text-[12.5px] font-semibold text-accent-ink">
            <Car className="h-3.5 w-3.5" aria-hidden="true" /> {t("vehicles.known")}: {vehicleLabel(t, known.make, known.model, known.color)}
          </p>
        )}
        {alreadyInside && (
          <div className="mt-2 flex items-center gap-2 rounded-lg bg-warn-soft px-3 py-2 text-[12.5px] font-bold text-warn-ink" data-testid="manual-already-inside">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            {t("manual.alreadyInside")}
            <button type="button" className="ml-auto underline" onClick={() => onOpenSession(alreadyInside)}>
              {t("live.open")}
            </button>
          </div>
        )}
      </div>
      <VehicleFields
        value={known?.known && !vehicle.make && !vehicle.model && !vehicle.color ? { make: known.make ?? "", model: known.model ?? "", color: known.color ?? "" } : vehicle}
        onChange={setVehicle}
      />
      <div>
        <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("manual.when")}</div>
        <div className="flex flex-wrap gap-2">
          {AGO.map((m) => (
            <button key={m} type="button" onClick={() => setAgo(m)} className={cn("rounded-full border px-3 py-1.5 text-[12.5px] font-bold", ago === m ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} aria-pressed={ago === m} data-testid={`manual-ago-${m}`}>
              {m === 0 ? t("manual.now") : t("manual.minutesAgo").replace("{n}", String(m))}
            </button>
          ))}
          <button type="button" onClick={() => setAgo("custom")} className={cn("rounded-full border px-3 py-1.5 text-[12.5px] font-bold", ago === "custom" ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} aria-pressed={ago === "custom"}>
            {t("manual.custom")}
          </button>
        </div>
        {ago === "custom" && <Input type="datetime-local" className="mt-2 font-mono" value={custom} onChange={(e) => setCustom(e.target.value)} aria-label={t("manual.custom")} />}
      </div>
      <div>
        <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("session.reason")}</div>
        <div className="flex flex-wrap gap-2">
          {REASONS.map((r) => (
            <button key={r} type="button" onClick={() => setReasonCode(reasonCode === r ? null : r)} className={cn("rounded-full border px-3 py-1.5 text-[12.5px] font-bold", reasonCode === r ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} aria-pressed={reasonCode === r}>
              {t(`manual.reasons.${r}`)}
            </button>
          ))}
        </div>
        <Label htmlFor="manual-comment" className="mt-2">
          {t("manual.comment")}
        </Label>
        <Input id="manual-comment" value={reasonText} onChange={(e) => setReasonText(e.target.value)} placeholder={t("manual.commentPh")} data-testid="manual-comment" />
      </div>
      {create.isError && <ErrorBox error={create.error} />}
      <div className="flex">
        <Button type="submit" size="lg" className="ml-auto" disabled={!canSubmit} data-testid="manual-submit">
          {t("manual.submit")}
        </Button>
      </div>
    </form>
  );
}
