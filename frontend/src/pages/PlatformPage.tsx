import { useMemo, useState } from "react";
import { Building2, Search } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import { usePlans, usePlatformTenants, useUpdatePlatformTenant, type PlatformTenantOut, type PlatformTenantUpdateRequest } from "@/api/hooks";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Drawer } from "@/components/ui/drawer";
import { Input, Label, Segmented, Select } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { SkeletonRows } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty";
import { toast } from "@/components/ui/toast";
import { ErrorBox } from "@/components/ErrorBox";
import { cn } from "@/lib/cn";
import { PlatformSummary } from "@/components/dashboard/PlatformSummary";

const TONE = { TRIAL: "blue", ACTIVE: "green", SUSPENDED: "red" } as const;
type Action = "paid" | "trial" | "suspend" | "resume" | "plan";

function addDays(from: Date, days: number): Date {
  return new Date(from.getTime() + days * 86_400_000);
}
function addMonths(from: Date, months: number): Date {
  const d = new Date(from);
  d.setMonth(d.getMonth() + months);
  return d;
}

/** Владелец платформы: клиенты, их тариф и статус, агрегаты использования (без данных водителей, A-14);
    отметка оплаты (наличные/перевод), продление пробного периода, приостановка. */
export function PlatformPage() {
  const { t, i18n } = useTranslation();
  const { me } = useAuth();
  const tenants = usePlatformTenants(true);
  const canManage = hasPermission(me, "tenants:manage");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<PlatformTenantOut | null>(null);
  const date = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleDateString(i18n.language === "uz" ? "uz-UZ" : "ru-RU") : "—");

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (tenants.data ?? []).filter((r) => !q || r.name.toLowerCase().includes(q) || r.code.toLowerCase().includes(q));
  }, [tenants.data, query]);

  return (
    <div className="space-y-4 py-2" data-testid="platform-page">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-[22px] font-extrabold">
            <Building2 className="h-6 w-6 text-accent" aria-hidden="true" /> {t("platform.title")}
          </h1>
          <p className="text-[13px] font-semibold text-muted">{t("platform.hint")}</p>
        </div>
      </div>
      <PlatformSummary />
      <Card className="p-0">
        <div className="flex items-center gap-2 border-b border-line p-3">
          <Search className="h-4 w-4 text-muted" aria-hidden="true" />
          <Input className="h-9 border-0 shadow-none" placeholder={t("platform.search")} aria-label={t("platform.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        {tenants.isPending ? (
          <div className="p-3">
            <SkeletonRows rows={5} />
          </div>
        ) : tenants.isError ? (
          <div className="p-3">
            <ErrorBox error={tenants.error} onRetry={() => void tenants.refetch()} />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState title={t("platform.empty")} />
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <thead>
                <tr>
                  <Th>{t("platform.client")}</Th>
                  <Th>{t("platform.plan")}</Th>
                  <Th>{t("platform.status")}</Th>
                  <Th>{t("platform.until")}</Th>
                  <Th>{t("platform.usage")}</Th>
                  <Th>{t("platform.users")}</Th>
                  <Th>{t("platform.sessions")}</Th>
                  {canManage && <Th />}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const until = r.status === "TRIAL" ? r.trial_ends_at : r.paid_until;
                  const overdue = until ? new Date(until).getTime() < Date.now() : false;
                  return (
                    <tr key={r.id} data-testid={`tenant-row-${r.code}`}>
                      <Td>
                        <div className="font-bold">{r.name}</div>
                        <div className="font-mono text-[11.5px] text-muted">
                          {r.code}
                          {r.contact_phone && <> · {r.contact_phone}</>}
                        </div>
                      </Td>
                      <Td className="font-bold">{r.plan_code}</Td>
                      <Td>
                        <Badge tone={TONE[r.status as keyof typeof TONE] ?? "gray"} className="whitespace-nowrap">{t(`sub.${r.status}`)}</Badge>
                      </Td>
                      <Td className={cn("whitespace-nowrap", overdue && "font-bold text-bad-ink")}>{date(until)}</Td>
                      <Td mono>
                        {r.lots} · {r.places} · {r.cameras}
                      </Td>
                      <Td mono>{r.users}</Td>
                      <Td mono>{r.sessions_30d}</Td>
                      {canManage && (
                        <Td>
                          <Button size="sm" variant="outline" onClick={() => setEditing(r)} data-testid={`tenant-manage-${r.code}`}>
                            {t("platform.manage")}
                          </Button>
                        </Td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          </div>
        )}
      </Card>
      <Drawer open={editing !== null} onClose={() => setEditing(null)} title={editing?.name ?? ""}>
        {editing && <ManageTenant tenant={editing} onDone={() => setEditing(null)} />}
      </Drawer>
    </div>
  );
}

function ManageTenant({ tenant, onDone }: { tenant: PlatformTenantOut; onDone: () => void }) {
  const { t, i18n } = useTranslation();
  const plans = usePlans();
  const update = useUpdatePlatformTenant();
  const [action, setAction] = useState<Action>(tenant.status === "SUSPENDED" ? "resume" : "paid");
  const [months, setMonths] = useState(1);
  const [days, setDays] = useState(7);
  const [plan, setPlan] = useState("");
  const [reason, setReason] = useState("");
  const now = new Date();
  const paidFrom = tenant.paid_until && new Date(tenant.paid_until) > now ? new Date(tenant.paid_until) : now;
  const trialFrom = tenant.trial_ends_at && new Date(tenant.trial_ends_at) > now ? new Date(tenant.trial_ends_at) : now;
  const newPaid = addMonths(paidFrom, months);
  const newTrial = addDays(trialFrom, days);
  const date = (d: Date) => d.toLocaleDateString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "numeric", month: "long", year: "numeric" });

  const body = (): PlatformTenantUpdateRequest => {
    const base = { reason: reason.trim(), plan_code: plan || null };
    switch (action) {
      case "paid":
        return { ...base, status: "ACTIVE", paid_until: newPaid.toISOString() };
      case "trial":
        return { ...base, status: "TRIAL", trial_ends_at: newTrial.toISOString() };
      case "suspend":
        return { ...base, status: "SUSPENDED" };
      case "resume":
        return { ...base, status: tenant.paid_until ? "ACTIVE" : "TRIAL" };
      case "plan":
        return base;
    }
  };
  const valid = reason.trim().length >= 3 && (action !== "plan" || plan !== "");
  const options: { value: Action; label: string }[] = [
    { value: "paid", label: t("platform.paid") },
    { value: "trial", label: t("platform.extendTrial") },
    tenant.status === "SUSPENDED" ? { value: "resume", label: t("platform.resume") } : { value: "suspend", label: t("platform.suspend") },
    { value: "plan", label: t("platform.changePlan") },
  ];

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid || update.isPending) return;
        update.mutate(
          { tenantId: tenant.id, body: body() },
          {
            onSuccess: () => {
              toast.ok(t("platform.saved"));
              onDone();
            },
          },
        );
      }}
      data-testid="tenant-manage-form"
    >
      <div className="text-[12.5px] font-semibold text-muted">
        {tenant.code} · {tenant.plan_code} · {t(`sub.${tenant.status}`)}
      </div>
      <Segmented<Action> ariaLabel={t("platform.action")} size="sm" value={action} onChange={setAction} options={options} />
      {action === "paid" && (
        <div>
          <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("platform.months")}</div>
          <div className="flex flex-wrap gap-2">
            {[1, 3, 6, 12].map((m) => (
              <button key={m} type="button" onClick={() => setMonths(m)} aria-pressed={months === m} className={cn("rounded-full border px-3 py-1.5 text-[13px] font-bold", months === m ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} data-testid={`months-${m}`}>
                {m}
              </button>
            ))}
          </div>
          <p className="mt-2 text-[13px] font-bold text-ok-ink" data-testid="paid-until-preview">
            {fmt(t("platform.paidUntilNew"), { date: date(newPaid) })}
          </p>
        </div>
      )}
      {action === "trial" && (
        <div>
          <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("platform.trialDays")}</div>
          <div className="flex flex-wrap gap-2">
            {[7, 14, 30].map((d) => (
              <button key={d} type="button" onClick={() => setDays(d)} aria-pressed={days === d} className={cn("rounded-full border px-3 py-1.5 text-[13px] font-bold", days === d ? "border-accent bg-accent-soft text-accent-ink" : "border-line")}>
                {d}
              </button>
            ))}
          </div>
          <p className="mt-2 text-[13px] font-bold text-accent-ink">{fmt(t("platform.trialUntilNew"), { date: date(newTrial) })}</p>
        </div>
      )}
      <div>
        <Label htmlFor="pt-plan">{t("platform.changePlan")}</Label>
        <Select id="pt-plan" value={plan} onChange={(e) => setPlan(e.target.value)} data-testid="pt-plan">
          <option value="">{t("platform.keepPlan")}</option>
          {(plans.data ?? []).map((p) => (
            <option key={p.code} value={p.code} disabled={p.code === tenant.plan_code}>
              {p.name}
            </option>
          ))}
        </Select>
      </div>
      <div>
        <Label htmlFor="pt-reason">{t("platform.reason")}</Label>
        <Input id="pt-reason" required minLength={3} placeholder={t("platform.reasonPh")} value={reason} onChange={(e) => setReason(e.target.value)} data-testid="pt-reason" />
      </div>
      {update.isError && <ErrorBox error={update.error} />}
      <div className="flex">
        <Button type="submit" size="lg" className="ml-auto" disabled={!valid || update.isPending} data-testid="pt-apply">
          {t("platform.apply")}
        </Button>
      </div>
    </form>
  );
}
