import { useDeferredValue, useMemo, useState, type FormEvent } from "react";
import { History, Moon, Wallet } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { PREVIEW_MINUTES, useLots, usePublishTariff, useTariffPlans, useTariffPreview, useTariffVersions, type TariffVersionOut } from "@/api/hooks";
import { hasPermission, useAuthStore } from "@/auth/store";
import { useLotStore } from "@/lib/lot";
import { formatMinor, formatTime } from "@/lib/format";
import { formFromRules, isActiveAt, rulesFromForm, type DayKind, type RoundingMode, type Rules, type TariffForm } from "@/lib/tariff";
import { cn } from "@/lib/cn";
import { Card, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Label, Segmented, Select } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { ErrorBox } from "@/components/ErrorBox";

const DAYS = [0, 1, 2, 3, 4, 5, 6] as const;
const STEPS = ["1", "5", "10", "15", "30", "60"];

/** Тариф площадки (настройка): что действует сейчас, новые цены с выбранного момента, пример расчёта, история. */
export function TariffPage() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const me = useAuthStore((st) => st.me);
  const canWrite = hasPermission(me, "tariff:write");
  const { lotId } = useLotStore();
  const lot = useLots().data?.find((l) => l.id === lotId);
  const plans = useTariffPlans(lotId);
  const plan = (plans.data ?? []).find((p) => p.is_default) ?? null;
  const versions = useTariffVersions(plan?.id ?? null);
  const now = Date.now();
  const sorted = useMemo(() => [...(versions.data ?? [])].sort((a, b) => b.version - a.version), [versions.data]);
  const current = sorted.find((v) => isActiveAt(v, now)) ?? null;

  if (!lotId) return <EmptyState title={t("tariffPage.noLot")} />;
  const pending = plans.isPending || (plan !== null && versions.isPending);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-2xl font-extrabold">{t("tariffPage.title")}</h1>
        {lot && <span className="text-[14px] font-semibold text-muted">{lot.name}</span>}
      </div>
      {(plans.isError || versions.isError) && <ErrorBox error={plans.error ?? versions.error} />}
      <Card data-testid="tariff-current">
        <CardTitle right={current ? <Badge tone="green">{fmt(t("tariffPage.version"), { n: current.version })}</Badge> : undefined}>
          <span className="flex items-center gap-2">
            <Wallet className="h-4 w-4 text-accent" aria-hidden="true" /> {t("tariffPage.current")}
          </span>
        </CardTitle>
        {pending ? (
          <SkeletonRows rows={2} />
        ) : current ? (
          <>
            <RulesSummary rules={current.rules} />
            <p className="mt-2 text-[12.5px] font-semibold text-muted">
              {fmt(t("tariffPage.since"), { from: formatTime(current.valid_from, lot?.timezone, lang) })} · {t("tariffPage.exitRule")}
            </p>
          </>
        ) : (
          <p className="rounded-lg bg-warn-soft px-3 py-2 text-[13px] font-semibold text-warn-ink" data-testid="tariff-none">
            {t("tariffPage.noTariff")}
          </p>
        )}
      </Card>
      {canWrite ? (
        !pending && <TariffEditor key={`${lotId}:${current?.id ?? "none"}`} lotId={lotId} planId={plan?.id ?? null} current={current} timeZone={lot?.timezone} />
      ) : (
        <p className="text-[13px] font-semibold text-muted">{t("tariffPage.readOnly")}</p>
      )}
      <TariffHistory versions={sorted} now={now} timeZone={lot?.timezone} />
    </div>
  );
}

function RulesSummary({ rules }: { rules: Rules }) {
  const { t, i18n } = useTranslation();
  const money = (v: unknown) => formatMinor(typeof v === "number" ? v : 0, i18n.language);
  const parts: string[] = [];
  const grace = Number(rules["grace_minutes"] ?? 0);
  if (grace > 0) parts.push(fmt(t("tariffPage.sGrace"), { n: grace }));
  parts.push(fmt(t("tariffPage.sFirst"), { v: money(rules["first_hour_price"]) }));
  parts.push(fmt(t("tariffPage.sPer"), { v: money(rules["price_per_hour"]) }));
  parts.push(fmt(t("tariffPage.sRound"), { mode: t(`tariffPage.mode${String(rules["rounding_mode"] ?? "UP")}`), n: Number(rules["rounding_step_minutes"] ?? 1) }));
  if (typeof rules["daily_max"] === "number") parts.push(fmt(t("tariffPage.sDaily"), { v: money(rules["daily_max"]) }));
  const night = rules["overnight"] as { start: string; end: string; price_per_hour: number } | null | undefined;
  if (night) parts.push(fmt(t("tariffPage.sNight"), { from: night.start, to: night.end, v: money(night.price_per_hour) }));
  const days = (rules["weekday_overrides"] ?? {}) as Record<string, { free?: boolean; price_per_hour?: number | null; first_hour_price?: number | null }>;
  for (const [d, o] of Object.entries(days)) {
    const day = t(`tariffPage.d${d}`);
    if (o.free) parts.push(fmt(t("tariffPage.sDayFree"), { day }));
    else parts.push(fmt(t("tariffPage.sDayCustom"), { day, v: money(o.price_per_hour ?? rules["price_per_hour"]) }));
  }
  return (
    <ul className="flex flex-wrap gap-2" data-testid="tariff-summary">
      {parts.map((p) => (
        <li key={p} className="rounded-lg bg-surface-2 px-2.5 py-1.5 text-[13.5px] font-bold text-ink">
          {p}
        </li>
      ))}
    </ul>
  );
}

function localNow(): string {
  const d = new Date(Date.now() - new Date().getTimezoneOffset() * 60_000);
  return d.toISOString().slice(0, 16);
}

function TariffEditor({ lotId, planId, current, timeZone }: { lotId: string; planId: string | null; current: TariffVersionOut | null; timeZone: string | undefined }) {
  const { t, i18n } = useTranslation();
  const [form, setForm] = useState<TariffForm>(() => formFromRules(current?.rules));
  const [when, setWhen] = useState<"now" | "at">("now");
  const [at, setAt] = useState(localNow);
  const [reason, setReason] = useState("");
  const publish = usePublishTariff();
  const rules = useMemo(() => rulesFromForm(form, current?.rules), [form, current]);
  const preview = useTariffPreview(lotId, useDeferredValue(rules));
  const set = <K extends keyof TariffForm>(key: K, value: TariffForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const setDay = (d: number, patch: Partial<TariffForm["days"][number]>) => setForm((f) => ({ ...f, days: f.days.map((x, i) => (i === d ? { ...x, ...patch } : x)) }));
  const atValid = when === "now" || !Number.isNaN(Date.parse(at));
  const valid = rules !== null && reason.trim().length >= 3 && atValid;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || publish.isPending) return;
    const validFrom = when === "now" ? new Date().toISOString() : new Date(at).toISOString();
    publish.mutate(
      { lotId, planId, rules: rules!, validFrom, reason: reason.trim() },
      { onSuccess: () => toast.ok(t("tariffPage.published")) },
    );
  };
  const field = (id: keyof TariffForm, label: string, placeholder?: string) => (
    <div>
      <Label htmlFor={`tf-${id}`}>{label}</Label>
      <Input id={`tf-${id}`} inputMode="decimal" className="font-mono" value={form[id] as string} placeholder={placeholder} onChange={(e) => set(id, e.target.value as never)} data-testid={`tf-${id}`} />
    </div>
  );

  return (
    <form onSubmit={submit} className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
      <Card>
        <CardTitle>{t("tariffPage.edit")}</CardTitle>
        <div className="grid gap-3 sm:grid-cols-2">
          {field("grace", t("tariffPage.grace"))}
          {field("firstHour", t("tariffPage.firstHour"))}
          {field("perHour", t("tariffPage.perHour"))}
          {field("dailyMax", t("tariffPage.dailyMax"), t("tariffPage.dailyMaxPh"))}
          <div>
            <Label htmlFor="tf-step">{t("tariffPage.roundStep")}</Label>
            <Select id="tf-step" value={form.step} onChange={(e) => set("step", e.target.value)}>
              {STEPS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <span className="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("tariffPage.roundMode")}</span>
            <Segmented<RoundingMode>
              ariaLabel={t("tariffPage.roundMode")}
              value={form.mode}
              onChange={(v) => set("mode", v)}
              options={(["UP", "NEAREST", "DOWN"] as const).map((m) => ({ value: m, label: t(`tariffPage.mode${m}`) }))}
            />
          </div>
        </div>

        <div className="mt-5 border-t border-line-2 pt-4">
          <label className="flex items-center gap-2 text-[14px] font-bold">
            <input type="checkbox" checked={form.night} onChange={(e) => set("night", e.target.checked)} data-testid="tf-night" />
            <Moon className="h-4 w-4 text-accent" aria-hidden="true" /> {t("tariffPage.night")}
          </label>
          {form.night && (
            <div className="mt-3 grid grid-cols-3 gap-3">
              {field("nightStart", t("tariffPage.nightFrom"), "22:00")}
              {field("nightEnd", t("tariffPage.nightTo"), "07:00")}
              {field("nightPrice", t("tariffPage.nightPrice"))}
            </div>
          )}
        </div>

        <div className="mt-5 border-t border-line-2 pt-4">
          <div className="mb-2 text-[14px] font-bold">{t("tariffPage.days")}</div>
          <div className="space-y-2">
            {DAYS.map((d) => {
              const day = form.days[d]!;
              return (
                <div key={d} className="grid grid-cols-[3rem_minmax(0,1fr)] items-center gap-2 sm:grid-cols-[3rem_12rem_minmax(0,1fr)]">
                  <span className="text-[13px] font-bold text-ink-2">{t(`tariffPage.d${d}`)}</span>
                  <Select aria-label={t(`tariffPage.d${d}`)} value={day.kind} onChange={(e) => setDay(d, { kind: e.target.value as DayKind })} data-testid={`tf-day-${d}`}>
                    <option value="base">{t("tariffPage.dayBase")}</option>
                    <option value="free">{t("tariffPage.dayFree")}</option>
                    <option value="custom">{t("tariffPage.dayCustom")}</option>
                  </Select>
                  {day.kind === "custom" && (
                    <div className="col-span-2 grid grid-cols-2 gap-2 sm:col-span-1">
                      <Input aria-label={t("tariffPage.firstHour")} inputMode="decimal" className="font-mono" placeholder={t("tariffPage.firstHour")} value={day.firstHour} onChange={(e) => setDay(d, { firstHour: e.target.value })} />
                      <Input aria-label={t("tariffPage.perHour")} inputMode="decimal" className="font-mono" placeholder={t("tariffPage.perHour")} value={day.perHour} onChange={(e) => setDay(d, { perHour: e.target.value })} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </Card>

      <div className="space-y-4">
        <Card data-testid="tariff-preview">
          <CardTitle>{t("tariffPage.preview")}</CardTitle>
          <p className="mb-2 text-[12px] font-semibold text-muted">{t("tariffPage.previewHint")}</p>
          {rules === null ? (
            <p className="text-[13px] font-semibold text-bad-ink">{t("tariffPage.invalid")}</p>
          ) : preview.isError ? (
            <ErrorBox error={preview.error} />
          ) : (
            <ul className={cn("divide-y divide-line-2", preview.isFetching && "opacity-60")}>
              {PREVIEW_MINUTES.map((m, i) => (
                <li key={m} className="flex items-center justify-between py-2 text-[14px]">
                  <span className="font-semibold text-ink-2">{t(`tariffPage.p${m}`)}</span>
                  <span className="font-mono font-bold" data-testid={`tf-preview-${m}`}>
                    {preview.data?.[i] ? formatMinor(preview.data[i].amount.minor, i18n.language) : "…"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card>
          <span className="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("tariffPage.when")}</span>
          <Segmented<"now" | "at">
            ariaLabel={t("tariffPage.when")}
            value={when}
            onChange={setWhen}
            options={[
              { value: "now", label: t("tariffPage.now") },
              { value: "at", label: t("tariffPage.at") },
            ]}
          />
          {when === "at" && <Input type="datetime-local" className="mt-2" aria-label={t("tariffPage.at")} value={at} onChange={(e) => setAt(e.target.value)} data-testid="tf-at" />}
          {when === "at" && timeZone && <p className="mt-1 text-[11.5px] font-semibold text-muted">{fmt(t("tariffPage.tzHint"), { tz: timeZone })}</p>}
          <div className="mt-3">
            <Label htmlFor="tf-reason">{t("common.reason")}</Label>
            <Input id="tf-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("tariffPage.reasonPh")} data-testid="tf-reason" />
          </div>
          {publish.isError && (
            <div className="mt-3">
              <ErrorBox error={publish.error} />
            </div>
          )}
          <Button type="submit" size="lg" className="mt-3 w-full" disabled={!valid || publish.isPending} data-testid="tf-publish">
            {t("tariffPage.publish")}
          </Button>
          <p className="mt-2 text-[11.5px] font-semibold text-muted">{t("tariffPage.exitRule")}</p>
        </Card>
      </div>
    </form>
  );
}

function TariffHistory({ versions, now, timeZone }: { versions: TariffVersionOut[]; now: number; timeZone: string | undefined }) {
  const { t, i18n } = useTranslation();
  if (versions.length === 0) return null;
  return (
    <Card data-testid="tariff-history">
      <CardTitle>
        <span className="flex items-center gap-2">
          <History className="h-4 w-4 text-accent" aria-hidden="true" /> {t("tariffPage.history")}
        </span>
      </CardTitle>
      <ul className="space-y-3">
        {versions.map((v) => {
          const active = isActiveAt(v, now);
          const future = Date.parse(v.valid_from) > now;
          return (
            <li key={v.id} className={cn("rounded-xl border p-3", active ? "border-ok bg-ok-soft/40" : "border-line-2")} data-testid="tariff-version">
              <div className="mb-2 flex flex-wrap items-center gap-2 text-[12.5px] font-semibold text-muted">
                <Badge tone={active ? "green" : future ? "yellow" : "gray"}>{fmt(t("tariffPage.version"), { n: v.version })}</Badge>
                <span>{fmt(t("tariffPage.since"), { from: formatTime(v.valid_from, timeZone, i18n.language) })}</span>
                {v.valid_to && <span>{fmt(t("tariffPage.until"), { to: formatTime(v.valid_to, timeZone, i18n.language) })}</span>}
                {active && <span className="font-bold text-ok-ink">{t("tariffPage.current")}</span>}
                {future && <span className="font-bold text-warn-ink">{t("tariffPage.scheduled")}</span>}
              </div>
              <RulesSummary rules={v.rules} />
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
