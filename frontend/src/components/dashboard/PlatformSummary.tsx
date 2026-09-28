import { Activity, AlertTriangle } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { usePlatformDashboard, type PlatformDashboardOut } from "@/api/hooks";
import { formatMinor } from "@/lib/format";
import { cn } from "@/lib/cn";
import { Card, CardTitle } from "@/components/ui/card";
import { Stat } from "@/components/ui/stat";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { BarChart } from "@/components/dashboard/charts";

/** 8 последних недель (с понедельника), недели без регистраций — нулём: пропуск на оси врал бы о динамике. */
function lastWeeks(n: number, rows: { week: string; count: number }[]): { week: string; count: number }[] {
  const byWeek = new Map(rows.map((r) => [r.week, r.count]));
  const now = new Date();
  const monday = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - ((now.getUTCDay() + 6) % 7)));
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(monday.getTime() - (n - 1 - i) * 7 * 86_400_000).toISOString().slice(0, 10);
    return { week: d, count: byWeek.get(d) ?? 0 };
  });
}

/* Сводка владельца платформы: клиенты по статусам, MRR, пробные, кому нужно внимание, регистрации по неделям и
   здоровье (Prometheus, если подключён, иначе локальные признаки). Только агрегаты — данных водителей нет. */
export function PlatformSummary() {
  const { t } = useTranslation();
  const q = usePlatformDashboard(true);
  if (q.isPending) return <Skeleton className="h-40 w-full" />;
  if (q.isError) return <ErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  return <SummaryView d={q.data} t={t} />;
}

function SummaryView({ d, t }: { d: PlatformDashboardOut; t: (k: string, o?: { defaultValue?: string }) => string }) {
  const { i18n } = useTranslation();
  const lang = i18n.language;
  const total = Object.values(d.clients).reduce((a, b) => a + b, 0);
  const date = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleDateString(lang === "uz" ? "uz-UZ" : "ru-RU") : "—");
  const weeks = lastWeeks(8, d.signups_by_week);
  return (
    <div className="space-y-4" data-testid="platform-summary">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={t("platform.dash.clients")} value={total} sub={fmt(t("platform.dash.paying"), { n: d.clients.ACTIVE ?? 0 })} testId="kpi-clients" />
        <Stat label="MRR" value={formatMinor(d.mrr, lang)} sub={d.contract_clients ? fmt(t("platform.dash.contract"), { n: d.contract_clients }) : undefined} testId="kpi-mrr" />
        <Stat label={t("platform.dash.trials")} value={d.trials} sub={d.trials_ending ? fmt(t("platform.dash.ending"), { n: d.trials_ending }) : undefined} tone={d.trials_ending ? "warn" : "ink"} />
        <Stat label={t("platform.dash.attention")} value={d.attention.length} tone={d.attention.length ? "warn" : "ok"} testId="kpi-attention" />
      </div>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)]">
        <Card>
          <CardTitle>
            <AlertTriangle className="mr-2 inline h-5 w-5 text-warn" aria-hidden="true" /> {t("platform.dash.attentionTitle")}
          </CardTitle>
          {d.attention.length === 0 ? (
            <p className="text-[13px] font-semibold text-muted">{t("platform.dash.allGood")}</p>
          ) : (
            <ul className="max-h-72 space-y-2 overflow-auto" data-testid="attention">
              {d.attention.map((a) => (
                <li key={a.tenant_id} className="rounded-xl border border-line p-2">
                  <div className="flex items-center gap-2 text-[13.5px] font-bold">
                    <span className="min-w-0 flex-1 truncate">{a.name}</span>
                    <span className="text-[12px] text-muted">{a.plan_code}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {a.reasons.map((r) => (
                      <Badge key={r} tone={r === "SUSPENDED" || r === "PAYMENT_OVERDUE" || r === "TRIAL_EXPIRED" ? "red" : "yellow"}>
                        {fmt(t(`platform.dash.reason.${r}`, { defaultValue: r }), { date: date(a.trial_ends_at ?? a.paid_until) })}
                      </Badge>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card>
          <CardTitle>{t("platform.dash.signups")}</CardTitle>
          {weeks.every((w) => w.count === 0) ? (
            <p className="text-[13px] font-semibold text-muted">{t("platform.dash.noSignups")}</p>
          ) : (
            <BarChart
              ariaLabel={t("platform.dash.signups")}
              series={[{ key: "n", label: t("platform.dash.signupsShort"), fill: "fill-series-1", dot: "bg-series-1" }]}
              columns={weeks.map((w) => ({ label: date(w.week).slice(0, 5), title: fmt(t("platform.dash.week"), { date: date(w.week) }), values: [w.count] }))}
              format={(v) => v.toFixed(0)}
              width={380}
              height={180}
              testId="chart-signups"
            />
          )}
          <div className="mt-3 flex flex-wrap gap-2 text-[12.5px] font-bold">
            {Object.entries(d.by_plan).map(([plan, n]) => (
              <span key={plan} className="rounded-lg bg-surface-2 px-2 py-1">
                {plan}: {n}
              </span>
            ))}
          </div>
        </Card>
        <HealthCard d={d} t={t} />
      </div>
    </div>
  );
}

function HealthCard({ d, t }: { d: PlatformDashboardOut; t: (k: string) => string }) {
  const p = d.prometheus;
  const rows: [string, string, boolean][] = p
    ? [
        [t("platform.dash.apiUp"), p.api_up !== null && p.api_up !== undefined ? String(p.api_up) : "—", !p.api_up],
        [t("platform.dash.p95"), p.api_p95_ms !== null && p.api_p95_ms !== undefined ? `${p.api_p95_ms} мс` : "—", (p.api_p95_ms ?? 0) > 150],
        [t("platform.dash.errors"), p.api_error_rate !== null && p.api_error_rate !== undefined ? `${(p.api_error_rate * 100).toFixed(2)}%` : "—", (p.api_error_rate ?? 0) > 0.01],
        [t("platform.dash.exitP95"), p.exit_p95_ms !== null && p.exit_p95_ms !== undefined ? `${p.exit_p95_ms} мс` : "—", (p.exit_p95_ms ?? 0) > 2000],
        [t("platform.dash.outboxLag"), p.outbox_lag_seconds !== null && p.outbox_lag_seconds !== undefined ? `${p.outbox_lag_seconds.toFixed(1)} с` : "—", (p.outbox_lag_seconds ?? 0) > 30],
      ]
    : [];
  const l = d.local;
  rows.push(
    [t("platform.dash.outbox"), `${l.outbox_pending}${l.outbox_oldest_seconds ? ` · ${l.outbox_oldest_seconds} с` : ""}`, (l.outbox_oldest_seconds ?? 0) > 60],
    [t("platform.dash.dlq"), d.dlq_length === null || d.dlq_length === undefined ? "—" : String(d.dlq_length), (d.dlq_length ?? 0) > 0],
    [t("platform.dash.webhooksDead"), String(l.webhooks_dead_24h), l.webhooks_dead_24h > 0],
    [t("platform.dash.camerasOffline"), `${l.cameras_offline} / ${l.cameras_total}`, l.cameras_offline > 0],
  );
  return (
    <Card data-testid="platform-health">
      <CardTitle right={<Badge tone={p ? "green" : "gray"}>{p ? "Prometheus" : t("platform.dash.localSource")}</Badge>}>
        <Activity className="mr-2 inline h-5 w-5" aria-hidden="true" /> {t("platform.dash.health")}
      </CardTitle>
      <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1.5 text-[13.5px]">
        {rows.map(([k, v, bad]) => (
          <div key={k} className="contents">
            <dt className="font-semibold text-muted">{k}</dt>
            <dd className={cn("text-right font-mono font-bold tabular-nums", bad && "text-bad-ink")}>{v}</dd>
          </div>
        ))}
      </dl>
      {p && p.firing_alerts.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1">
          {p.firing_alerts.map((a) => (
            <Badge key={a} tone="red">
              {a}
            </Badge>
          ))}
        </div>
      )}
      {!p && <p className="mt-3 text-[12px] font-semibold text-muted">{t("platform.dash.localHint")}</p>}
    </Card>
  );
}
