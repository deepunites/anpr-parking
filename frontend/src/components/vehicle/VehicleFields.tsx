import { useId } from "react";
import { useTranslation } from "@/i18n";
import { Input, Label } from "@/components/ui/input";
import { cn } from "@/lib/cn";
import { COLORS, MAKES, MAKE_NAMES, findMake } from "./catalog";
import type { VehicleValue } from "./vehicle";


/** Марка — с подсказками популярных марок, модель — подсказки по выбранной марке, цвет — кружками.
    Всё необязательно: оператор заполняет то, что видит. */
export function VehicleFields({ value, onChange, compact }: { value: VehicleValue; onChange: (v: VehicleValue) => void; compact?: boolean }) {
  const { t } = useTranslation();
  const uid = useId();
  const make = findMake(value.make);
  const models = make ? MAKES[make]! : [];
  const quickMakes = ["Chevrolet", "BYD", "Hyundai", "Kia", "Toyota", "Lada"];
  return (
    <div className="flex flex-col gap-1" data-testid="vehicle-fields">
      <div className={cn("grid gap-3", compact ? "grid-cols-2" : "grid-cols-1 sm:grid-cols-2")}>
        <div>
          <Label htmlFor={`${uid}-make`}>{t("vehicle.make")}</Label>
          <Input id={`${uid}-make`} list={`${uid}-makes`} value={value.make} onChange={(e) => onChange({ ...value, make: e.target.value, model: findMake(e.target.value) === make ? value.model : "" })} placeholder="Chevrolet" autoComplete="off" data-testid="vehicle-make" />
          <datalist id={`${uid}-makes`}>
            {MAKE_NAMES.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </div>
        <div>
          <Label htmlFor={`${uid}-model`}>{t("vehicle.model")}</Label>
          <Input id={`${uid}-model`} list={`${uid}-models`} value={value.model} onChange={(e) => onChange({ ...value, model: e.target.value })} placeholder={models[0] ?? "Cobalt"} autoComplete="off" data-testid="vehicle-model" />
          <datalist id={`${uid}-models`}>
            {models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </div>
      </div>
      {!compact && !value.make && (
        <div className="mt-1 flex flex-wrap gap-1.5" aria-label={t("vehicle.quickMakes")}>
          {quickMakes.map((m) => (
            <button key={m} type="button" onClick={() => onChange({ ...value, make: m, model: "" })} className="rounded-full border border-line px-2.5 py-1 text-[12px] font-bold text-ink-2 hover:bg-surface-2" data-testid={`quick-make-${m}`}>
              {m}
            </button>
          ))}
        </div>
      )}
      {models.length > 0 && !value.model && (
        <div className="mt-1 flex flex-wrap gap-1.5" aria-label={t("vehicle.quickModels")}>
          {models.slice(0, 8).map((m) => (
            <button key={m} type="button" onClick={() => onChange({ ...value, model: m })} className="rounded-full bg-surface-2 px-2.5 py-1 text-[12px] font-bold text-ink-2 hover:bg-surface-3" data-testid={`quick-model-${m}`}>
              {m}
            </button>
          ))}
        </div>
      )}
      <div className="mt-2 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("vehicle.color")}</div>
      <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={t("vehicle.color")}>
        {COLORS.map((c) => (
          <button
            key={c.code}
            type="button"
            role="radio"
            aria-checked={value.color === c.code}
            title={t(`colors.${c.code}`)}
            onClick={() => onChange({ ...value, color: value.color === c.code ? "" : c.code })}
            className={cn("flex items-center gap-1.5 rounded-full border px-2 py-1 text-[12px] font-bold", value.color === c.code ? "border-accent bg-accent-soft text-accent-ink" : "border-line text-ink-2 hover:bg-surface-2")}
            data-testid={`color-${c.code}`}
          >
            <i aria-hidden="true" className={cn("h-3.5 w-3.5 rounded-full border border-black/15", c.swatch)} />
            {t(`colors.${c.code}`)}
          </button>
        ))}
      </div>
    </div>
  );
}
