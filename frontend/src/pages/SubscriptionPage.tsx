import { useState } from "react";
import { Link } from "react-router-dom";
import { Banknote, CarFront, Rocket } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useChangePlan, useSubscription, type PlatformPlanOut } from "@/api/hooks";
import { Card, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { ErrorBox } from "@/components/ErrorBox";
import { PlanCards } from "@/components/billing/PlanCards";
import { cn } from "@/lib/cn";

const TONE = { TRIAL: "blue", ACTIVE: "green", SUSPENDED: "red" } as const;

/** Подписка клиента на платформу: статус, пробный период, использование лимитов, смена тарифа, как оплатить. */
export function SubscriptionPage() {
  const { t, i18n } = useTranslation();
  const sub = useSubscription(true);
  const change = useChangePlan();
  const [target, setTarget] = useState<PlatformPlanOut | null>(null);

  if (sub.isPending) return <Skeleton className="mt-4 h-72 w-full" />;
  if (sub.isError) return <ErrorBox error={sub.error} onRetry={() => void sub.refetch()} />;
  const s = sub.data;
  const date = (iso: string) => new Date(iso).toLocaleDateString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "numeric", month: "long", year: "numeric" });
  const usage: { key: "lots" | "places" | "cameras"; used: number; limit: number | null | undefined }[] = [
    { key: "lots", used: s.usage.lots, limit: s.plan.max_lots },
    { key: "places", used: s.usage.places, limit: s.plan.max_places },
    { key: "cameras", used: s.usage.cameras, limit: s.plan.max_cameras },
  ];
  const fits = (p: PlatformPlanOut) =>
    (p.max_lots == null || s.usage.lots <= p.max_lots) && (p.max_places == null || s.usage.places <= p.max_places) && (p.max_cameras == null || s.usage.cameras <= p.max_cameras);

  return (
    <div className="mx-auto max-w-4xl space-y-4 py-2" data-testid="subscription-page">
      <h1 className="text-[22px] font-extrabold">{t("sub.title")}</h1>
      {s.blocked_reason && (
        <div role="alert" className="rounded-xl border border-bad/50 bg-bad-soft p-3 text-[13.5px] font-bold text-bad-ink">
          {s.blocked_reason === "SUSPENDED" ? t("sub.bannerSuspended") : t("sub.bannerExpired")}
        </div>
      )}
      <div className="grid gap-4 md:grid-cols-[1.2fr_1fr]">
        <Card>
          <CardTitle right={<Badge tone={TONE[s.status as keyof typeof TONE] ?? "gray"}>{t(`sub.${s.status}`)}</Badge>}>
            {t("sub.plan")}: {s.plan.name}
          </CardTitle>
          <div className="mb-4 text-[13.5px] font-semibold text-ink-2" data-testid="sub-status-line">
            {s.status === "TRIAL" && s.trial_days_left != null && <>{fmt(t("sub.trialLeft"), { n: s.trial_days_left })}{s.trial_ends_at && <> · {date(s.trial_ends_at)}</>}</>}
            {s.status !== "TRIAL" && s.paid_until && fmt(t("sub.paidUntil"), { date: date(s.paid_until) })}
          </div>
          <div className="mb-2 text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t("sub.usage")}</div>
          <div className="space-y-3">
            {usage.map((u) => {
              const pct = u.limit ? Math.min(100, Math.round((u.used / u.limit) * 100)) : 0;
              return (
                <div key={u.key} data-testid={`usage-${u.key}`}>
                  <div className="mb-1 flex text-[13px] font-bold">
                    <span>{t(`sub.${u.key}`)}</span>
                    <span className="ml-auto font-mono">
                      {u.used} / {u.limit ?? "∞"}
                    </span>
                  </div>
                  {/* <progress>, а не div с шириной: CSP запрещает inline-стили, а ширина — данные */}
                  <progress
                    max={u.limit ?? 1}
                    value={u.limit ? Math.min(u.used, u.limit) : 0}
                    aria-label={t(`sub.${u.key}`)}
                    className={cn(
                      "block h-2 w-full appearance-none overflow-hidden rounded-full bg-surface-2 [&::-webkit-progress-bar]:bg-surface-2 [&::-webkit-progress-value]:rounded-full",
                      pct >= 100 ? "[&::-moz-progress-bar]:bg-bad [&::-webkit-progress-value]:bg-bad" : pct >= 80 ? "[&::-moz-progress-bar]:bg-warn [&::-webkit-progress-value]:bg-warn" : "[&::-moz-progress-bar]:bg-accent [&::-webkit-progress-value]:bg-accent",
                    )}
                  />
                </div>
              );
            })}
          </div>
          {s.status === "TRIAL" && (
            <Link to="/welcome" className="mt-4 inline-flex items-center gap-2 text-[13.5px] font-bold text-accent-ink underline">
              <Rocket className="h-4 w-4" aria-hidden="true" /> {t("sub.setup")}
            </Link>
          )}
        </Card>
        <Card>
          <CardTitle>
            <span className="flex items-center gap-2">
              <Banknote className="h-5 w-5 text-accent" aria-hidden="true" /> {t("sub.howToPay")}
            </span>
          </CardTitle>
          <p className="mb-3 text-[13.5px] font-semibold text-ink-2">{t("sub.payHint")}</p>
          <p className="flex items-start gap-2 rounded-xl bg-surface-2 p-3 text-[12.5px] font-semibold text-ink-2">
            <CarFront className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> {t("sub.passageNote")}
          </p>
        </Card>
      </div>
      <Card>
        <CardTitle>{t("sub.plans")}</CardTitle>
        <PlanCards plans={s.plans} value={s.plan.code} current={s.plan.code} disabled={(p) => !fits(p)} onChange={(code) => code !== s.plan.code && setTarget(s.plans.find((p) => p.code === code) ?? null)} />
      </Card>
      <Dialog open={target !== null} onClose={() => { setTarget(null); change.reset(); }} title={target ? fmt(t("sub.confirmChange"), { plan: target.name }) : ""}>
        {change.isError && <ErrorBox error={change.error} />}
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => { setTarget(null); change.reset(); }}>
            {t("common.no")}
          </Button>
          <Button
            disabled={change.isPending}
            onClick={() =>
              target &&
              change.mutate(
                { plan_code: target.code, reason: t("sub.reason") },
                {
                  onSuccess: () => {
                    toast.ok(t("sub.changed"));
                    setTarget(null);
                  },
                },
              )
            }
            data-testid="sub-change-confirm"
          >
            {t("common.yes")}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
