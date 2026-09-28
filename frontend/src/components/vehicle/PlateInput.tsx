import { forwardRef, useId } from "react";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { useTranslation } from "@/i18n";
import { detectFormat, formatPlate, invalidChars, normalizePlate, uzProgress } from "@/lib/plate";
import { cn } from "@/lib/cn";

/** Поле номера в виде таблички: русские буквы превращаются в латинские (А→A, В→B…), пробелы расставляются как на
    номере, под полем — какой это формат или чего не хватает. Наружу отдаётся нормализованный номер без пробелов. */
export const PlateInput = forwardRef<
  HTMLInputElement,
  { value: string; onChange: (normalized: string) => void; label?: string; id?: string; size?: "md" | "lg"; testId?: string; autoFocus?: boolean }
>(({ value, onChange, label, id, size = "lg", testId = "plate-input", autoFocus }, ref) => {
  const { t } = useTranslation();
  const auto = useId();
  const inputId = id ?? `plate-${auto}`;
  const fmt = detectFormat(value);
  const bad = invalidChars(value);
  const progress = uzProgress(value);
  let hint: { ok: boolean; text: string } | null = null;
  if (value.length > 0) {
    if (bad.length > 0) hint = { ok: false, text: `${t("plate.badChars")}: ${bad.join(" ")}` };
    else if (fmt === "UZ_STANDARD") hint = { ok: true, text: t("plate.uzStandard") };
    else if (fmt === "UZ_LEGAL") hint = { ok: true, text: t("plate.uzLegal") };
    else if (progress) hint = { ok: false, text: t("plate.missing").replace("{n}", String(progress.missing)) };
    else if (fmt === "FOREIGN") hint = { ok: true, text: t("plate.foreign") };
    else hint = { ok: false, text: t("plate.tooShort") };
  }
  return (
    <div>
      {label && (
        <label htmlFor={inputId} className="mb-1 block text-[11px] font-bold uppercase tracking-[0.06em] text-muted">
          {label}
        </label>
      )}
      <div
        className={cn(
          "flex items-stretch overflow-hidden rounded-[10px] border-2 bg-[color:var(--plate-bg)] shadow-[inset_0_0_0_2px_var(--plate-bg)] focus-within:shadow-[0_0_0_4px_color-mix(in_srgb,var(--accent)_16%,transparent)]",
          hint && !hint.ok ? "border-warn" : "border-[color:var(--plate-ink)]",
        )}
      >
        <span aria-hidden="true" className={cn("flex flex-col items-center justify-center bg-[#2b6cf6] px-2 font-sans font-extrabold text-white", size === "lg" ? "text-[12px]" : "text-[10px]")}>
          UZ
        </span>
        <input
          ref={ref}
          id={inputId}
          value={formatPlate(value)}
          onChange={(e) => onChange(normalizePlate(e.target.value))}
          className={cn(
            "w-full min-w-0 bg-transparent px-3 font-mono font-bold uppercase tracking-[0.08em] text-[color:var(--plate-ink)] placeholder:font-semibold placeholder:tracking-normal placeholder:text-[#9aa7bb] focus:outline-none",
            size === "lg" ? "h-14 text-[26px]" : "h-11 text-[19px]",
          )}
          placeholder="01 A 777 AA"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          autoFocus={autoFocus}
          maxLength={16}
          aria-invalid={hint ? !hint.ok : undefined}
          data-testid={testId}
        />
      </div>
      {hint && (
        <p className={cn("mt-1 flex items-center gap-1.5 text-[12.5px] font-semibold", hint.ok ? "text-ok-ink" : "text-warn-ink")} data-testid={`${testId}-hint`}>
          {hint.ok ? <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" /> : <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />}
          {hint.text}
        </p>
      )}
    </div>
  );
});
PlateInput.displayName = "PlateInput";

// eslint-disable-next-line react-refresh/only-export-components -- проверка рядом с полем
export function isPlateValid(value: string): boolean {
  return detectFormat(value) !== null && invalidChars(value).length === 0;
}
