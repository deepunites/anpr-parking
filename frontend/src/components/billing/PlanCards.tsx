import { Check } from "lucide-react";
import { fmt, plural, useTranslation } from "@/i18n";
import type { PlatformPlanOut } from "@/api/hooks";
import { cn } from "@/lib/cn";

const sum = (minor: number) => new Intl.NumberFormat("ru-RU").format(Math.round(minor / 100));

/** Карточки тарифов платформы: регистрация и экран «Подписка». Выбор — радиогруппа (клавиатура, скринридер). */
export function PlanCards({
  plans,
  value,
  onChange,
  current,
  disabled,
}: {
  plans: PlatformPlanOut[];
  value: string | null;
  onChange: (code: string) => void;
  current?: string | null;
  disabled?: (p: PlatformPlanOut) => boolean;
}) {
  const { t } = useTranslation();
  const limit = (n: number | null | undefined, forms: string) => (n == null ? t("signup.unlimited") : `${n} ${plural(n, forms)}`);
  return (
    <div role="radiogroup" aria-label={t("signup.choosePlan")} className="grid gap-3 sm:grid-cols-3">
      {plans.map((p) => {
        const selected = value === p.code;
        const off = disabled?.(p) ?? false;
        return (
          <button
            key={p.code}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={off}
            onClick={() => onChange(p.code)}
            className={cn(
              "relative flex flex-col items-start gap-2 rounded-2xl border-2 bg-surface p-4 text-left transition-colors",
              selected ? "border-accent shadow-card" : "border-line hover:border-accent/50",
              off && "cursor-not-allowed opacity-50",
            )}
            data-testid={`plan-${p.code}`}
          >
            <div className="flex w-full items-center gap-2">
              <span className="text-[16px] font-extrabold">{p.name}</span>
              {current === p.code && <span className="rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] font-bold text-muted">{t("sub.current")}</span>}
              {selected && <Check className="ml-auto h-5 w-5 text-accent" aria-hidden="true" />}
            </div>
            <div className="text-[22px] font-extrabold leading-none">
              {p.price_minor == null ? (
                <span className="text-[16px]">{t("signup.onRequest")}</span>
              ) : (
                <>
                  {sum(p.price_minor)} <span className="text-[12.5px] font-bold text-muted">{t("signup.perMonth")}</span>
                </>
              )}
            </div>
            <ul className="space-y-0.5 text-[13px] font-semibold text-ink-2">
              <li>{limit(p.max_lots, t("signup.lots"))}</li>
              <li>{limit(p.max_places, t("signup.places"))}</li>
              <li>{limit(p.max_cameras, t("signup.cameras"))}</li>
            </ul>
            <span className="rounded-full bg-ok-soft px-2 py-0.5 text-[11.5px] font-bold text-ok-ink">{fmt(t("signup.trialBadge"), { n: p.trial_days })}</span>
          </button>
        );
      })}
    </div>
  );
}
