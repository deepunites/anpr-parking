import { useEffect, useMemo, useState } from "react";
import { Ban, Car, Crown, Plus, Printer, Search, ShieldCheck, Ticket } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import { useAddRule, useCreateSubscription, useLots, usePrintSubscriptionReceipt, useRevokeRule, useRules, useVehicleLookup, type RuleOut } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Drawer } from "@/components/ui/drawer";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { Tabs } from "@/components/ui/tabs";
import { PlateInput, isPlateValid } from "@/components/vehicle/PlateInput";
import { VehicleFields } from "@/components/vehicle/VehicleFields";
import { EMPTY_VEHICLE, colorSwatch, vehicleLabel, type VehicleValue } from "@/components/vehicle/vehicle";
import { normalizePlate } from "@/lib/plate";
import { cn } from "@/lib/cn";

type Kind = "WHITELIST" | "VIP" | "SUBSCRIPTION" | "BLACKLIST";
const KINDS: { kind: Kind; icon: React.ReactNode; tone: "green" | "blue" | "yellow" | "red" }[] = [
  { kind: "WHITELIST", icon: <ShieldCheck className="h-4 w-4" aria-hidden="true" />, tone: "green" },
  { kind: "VIP", icon: <Crown className="h-4 w-4" aria-hidden="true" />, tone: "blue" },
  { kind: "SUBSCRIPTION", icon: <Ticket className="h-4 w-4" aria-hidden="true" />, tone: "yellow" },
  { kind: "BLACKLIST", icon: <Ban className="h-4 w-4" aria-hidden="true" />, tone: "red" },
];
const PERIODS: { id: string; months: number | null }[] = [
  { id: "forever", months: null },
  { id: "m1", months: 1 },
  { id: "m3", months: 3 },
  { id: "m6", months: 6 },
  { id: "m12", months: 12 },
];

function addMonths(d: Date, m: number): Date {
  const out = new Date(d);
  out.setMonth(out.getMonth() + m);
  return out;
}

function dateOnly(iso: string | null | undefined, lang: string): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(lang === "uz" ? "uz-UZ" : "ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

/** Площадка для номера чека абонемента: выбранная в шапке, иначе первая. */
function useReceiptLot(): string | null {
  const { lotId } = useLotStore();
  const lots = useLots();
  return lotId ?? lots.data?.[0]?.id ?? null;
}

function receiptToasts(t: (key: string) => string) {
  return {
    onSuccess: (r: { receipt_no: string }) => toast.ok(fmt(t("session.receiptPrinted"), { n: r.receipt_no })),
    onError: () => toast.warn(t("session.receiptFailed")),
  };
}

/* Машины и доступ: белый список, VIP, абонементы, чёрный список — с маркой, моделью и цветом. Добавление — одна
   форма: номер (кириллица сама становится латиницей), марка с подсказками, что разрешить и на какой срок. */
export function VehiclesPage() {
  const { t, i18n } = useTranslation();
  const { me } = useAuth();
  const canWrite = hasPermission(me, "access:write");
  // месячный абонемент продаёт только администратор (MediaPark, 2026-10-19)
  const canSell = hasPermission(me, "passes:sell");
  const printSub = usePrintSubscriptionReceipt();
  const receiptLot = useReceiptLot();
  const rules = useRules(true);
  const lots = useLots();
  const [tab, setTab] = useState<"all" | Kind>("all");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [revoking, setRevoking] = useState<RuleOut | null>(null);

  const active = useMemo(() => (rules.data?.items ?? []).filter((r) => !r.revoked_at), [rules.data]);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: active.length };
    for (const r of active) c[r.kind] = (c[r.kind] ?? 0) + 1;
    return c;
  }, [active]);
  const q = normalizePlate(query);
  const qText = query.trim().toLowerCase();
  const visible = active.filter(
    (r) =>
      (tab === "all" || r.kind === tab) &&
      (!query.trim() || r.plate.includes(q) || (r.vehicle_make ?? "").toLowerCase().includes(qText) || (r.vehicle_model ?? "").toLowerCase().includes(qText) || (r.reason ?? "").toLowerCase().includes(qText)),
  );
  const lotName = (id: string | null) => (id ? (lots.data?.find((l) => l.id === id)?.name ?? id.slice(0, 8)) : t("vehicles.allLots"));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.access")}</h1>
        {canWrite && (
          <Button className="ml-auto" onClick={() => setAdding(true)} data-testid="add-vehicle">
            <Plus className="h-4 w-4" aria-hidden="true" /> {t("vehicles.add")}
          </Button>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <Tabs
            items={[
              { id: "all", label: t("vehicles.all"), count: counts["all"] },
              ...KINDS.map((k) => ({ id: k.kind, label: t(`vehicles.kind.${k.kind}`), count: counts[k.kind] ?? 0 })),
            ]}
            value={tab}
            onChange={(id) => setTab(id as typeof tab)}
          />
        </div>
        <div className="relative w-full sm:w-72">
          <Search className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-accent" aria-hidden="true" />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} className="pl-9" placeholder={t("vehicles.searchPh")} aria-label={t("vehicles.search")} data-testid="vehicles-search" />
        </div>
      </div>
      <Card>
        {rules.isPending && <SkeletonRows />}
        {rules.isError && <ErrorBox error={rules.error} onRetry={() => void rules.refetch()} />}
        {rules.data && visible.length === 0 && (
          <EmptyState
            title={query ? t("live.noResults") : t("vehicles.empty")}
            hint={query ? undefined : t("vehicles.emptyHint")}
            action={
              canWrite && !query ? (
                <Button onClick={() => setAdding(true)}>
                  <Plus className="h-4 w-4" aria-hidden="true" /> {t("vehicles.add")}
                </Button>
              ) : undefined
            }
          />
        )}
        {rules.data && visible.length > 0 && (
          <Table data-testid="rules-table">
            <thead>
              <tr>
                <Th>{t("session.plate")}</Th>
                <Th>{t("vehicles.car")}</Th>
                <Th>{t("vehicles.access")}</Th>
                <Th>{t("vehicles.lot")}</Th>
                <Th>{t("vehicles.period")}</Th>
                <Th>{t("vehicles.note")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => {
                const k = KINDS.find((x) => x.kind === r.kind);
                const label = vehicleLabel(t, r.vehicle_make, r.vehicle_model, r.vehicle_color);
                const sw = colorSwatch(r.vehicle_color);
                return (
                  <tr key={r.id} data-testid="rule-row" data-plate={r.plate}>
                    <Td>
                      <Plate plate={r.plate} size="sm" highlight={q.length >= 2 ? q : undefined} />
                    </Td>
                    <Td>
                      {label ? (
                        <span className="inline-flex items-center gap-2 font-bold">
                          {sw && <i aria-hidden="true" className={cn("h-3 w-3 rounded-full border border-black/15", sw)} />}
                          {label}
                        </span>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </Td>
                    <Td>
                      <Badge tone={k?.tone ?? "gray"}>
                        {k?.icon} {t(`vehicles.kind.${r.kind}`)}
                        {r.kind === "WHITELIST" && r.discount_percent < 100 ? ` −${r.discount_percent}%` : ""}
                      </Badge>
                    </Td>
                    <Td className="text-[12.5px]">{lotName(r.lot_id)}</Td>
                    <Td mono>
                      {dateOnly(r.valid_from, i18n.language)} → {r.valid_to ? dateOnly(r.valid_to, i18n.language) : t("vehicles.forever")}
                    </Td>
                    <Td className="max-w-[220px] truncate text-[12.5px] text-ink-2">{r.reason ?? ""}</Td>
                    <Td>
                      {canSell && r.kind === "SUBSCRIPTION" && r.source_id && receiptLot && (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={printSub.isPending}
                          onClick={() => printSub.mutate({ subscriptionId: r.source_id!, lotId: r.lot_id ?? receiptLot }, receiptToasts(t))}
                          data-testid="subscription-receipt"
                        >
                          <Printer className="h-4 w-4" aria-hidden="true" /> {t("session.printReceipt")}
                        </Button>
                      )}
                      {canWrite && (
                        <Button variant="ghost" size="sm" onClick={() => setRevoking(r)} data-testid="revoke-rule">
                          {t("vehicles.revoke")}
                        </Button>
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>

      <Drawer open={adding} onClose={() => setAdding(false)} title={t("vehicles.add")} width="w-[520px]">
        <AddVehicleForm canSell={canSell} onDone={() => setAdding(false)} />
      </Drawer>

      <Dialog open={revoking !== null} onClose={() => setRevoking(null)} title={t("vehicles.revokeTitle")}>
        {revoking && <RevokeForm rule={revoking} onDone={() => setRevoking(null)} />}
      </Dialog>
    </div>
  );
}

function RevokeForm({ rule, onDone }: { rule: RuleOut; onDone: () => void }) {
  const { t } = useTranslation();
  const revoke = useRevokeRule();
  const [reason, setReason] = useState("");
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (reason.trim().length >= 3)
          revoke.mutate(
            { ruleId: rule.id, reason: reason.trim() },
            {
              onSuccess: () => {
                toast.ok(t("vehicles.revoked"));
                onDone();
              },
            },
          );
      }}
    >
      <div className="flex items-center gap-2">
        <Plate plate={rule.plate} size="sm" />
        <Badge tone="gray">{t(`vehicles.kind.${rule.kind}`)}</Badge>
      </div>
      <div>
        <Label htmlFor="revoke-reason">{t("session.reason")}</Label>
        <Textarea id="revoke-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("vehicles.revokePh")} autoFocus data-testid="revoke-reason" />
      </div>
      <ErrorBox error={revoke.error} />
      <Button type="submit" variant="danger" disabled={reason.trim().length < 3 || revoke.isPending} data-testid="revoke-submit">
        {t("vehicles.revoke")}
      </Button>
    </form>
  );
}

function AddVehicleForm({ onDone, canSell }: { onDone: () => void; canSell: boolean }) {
  const { t } = useTranslation();
  const lots = useLots();
  const addRule = useAddRule();
  const addSub = useCreateSubscription();
  const printSub = usePrintSubscriptionReceipt();
  const receiptLot = useReceiptLot();
  const [plate, setPlate] = useState("");
  const [vehicle, setVehicle] = useState<VehicleValue>(EMPTY_VEHICLE);
  const [kind, setKind] = useState<Kind>("WHITELIST");
  const [lotId, setLotId] = useState<string>("");
  const [period, setPeriod] = useState("m1");
  const [discount, setDiscount] = useState(100);
  const [price, setPrice] = useState("300000");
  const [note, setNote] = useState("");
  const valid = isPlateValid(plate);
  const lookup = useVehicleLookup(plate, valid);
  // машина уже известна — подставляем марку, если оператор её ещё не вводил
  useEffect(() => {
    const v = lookup.data;
    if (v?.known && !vehicle.make && !vehicle.model && !vehicle.color) setVehicle({ make: v.make ?? "", model: v.model ?? "", color: v.color ?? "" });
  }, [lookup.data, vehicle]);

  const months = PERIODS.find((p) => p.id === period)?.months ?? null;
  const effectiveMonths = kind === "SUBSCRIPTION" ? (months ?? 1) : months;
  const now = new Date();
  const until = effectiveMonths ? addMonths(now, effectiveMonths) : null;
  const busy = addRule.isPending || addSub.isPending;
  const noteOk = note.trim().length >= 3;
  const canSubmit = valid && noteOk && !busy && (kind !== "SUBSCRIPTION" || Number(price) >= 0);

  const submit = () => {
    const attrs = { make: vehicle.make.trim() || null, model: vehicle.model.trim() || null, color: vehicle.color || null };
    const done = {
      onSuccess: () => {
        toast.ok(`${t("vehicles.added")} · ${t(`vehicles.kind.${kind}`)}`);
        onDone();
      },
    };
    if (kind === "SUBSCRIPTION") {
      addSub.mutate(
        { plate, lot_id: lotId || null, plan_code: effectiveMonths === 12 ? "YEAR" : `M${effectiveMonths}`, starts_at: now.toISOString(), ends_at: until!.toISOString(), price_minor: Math.round(Number(price) * 100), reason: note.trim(), ...attrs },
        {
          onSuccess: (rule) => {
            done.onSuccess();
            // продан — сразу чек с номером; принтер выключен — абонемент всё равно продан, чек можно напечатать из списка
            const lot = lotId || receiptLot;
            if (rule.source_id && lot) printSub.mutate({ subscriptionId: rule.source_id, lotId: lot }, receiptToasts(t));
          },
        },
      );
    } else {
      addRule.mutate(
        { plate, kind, lot_id: lotId || null, discount_percent: kind === "WHITELIST" ? discount : 0, valid_to: until ? until.toISOString() : null, note: note.trim(), reason: note.trim(), ...attrs },
        done,
      );
    }
  };

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSubmit) submit();
      }}
      data-testid="vehicle-form"
    >
      <div>
        <PlateInput value={plate} onChange={setPlate} label={t("session.plate")} autoFocus />
        {lookup.data?.known && (
          <p className="mt-1 flex items-center gap-1.5 text-[12.5px] font-semibold text-accent-ink" data-testid="vehicle-known">
            <Car className="h-3.5 w-3.5" aria-hidden="true" />
            {t("vehicles.known")}
            {vehicleLabel(t, lookup.data.make, lookup.data.model, lookup.data.color) ? `: ${vehicleLabel(t, lookup.data.make, lookup.data.model, lookup.data.color)}` : ""}
          </p>
        )}
        {/* опечатка в номере — и VIP/абонемент не сработают (MediaPark: VIP на 90U475BA, камеры читали 90Q475UA) */}
        {lookup.data && !lookup.data.known && (
          <p className="mt-1 rounded-lg bg-warn-soft px-2.5 py-1.5 text-[12.5px] font-semibold text-warn-ink" data-testid="vehicle-unseen">
            {t("vehicles.unseen")}
          </p>
        )}
      </div>
      <VehicleFields value={vehicle} onChange={setVehicle} />

      <div>
        <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("vehicles.whatAllow")}</div>
        <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label={t("vehicles.whatAllow")}>
          {KINDS.filter((k) => k.kind !== "SUBSCRIPTION" || canSell).map((k) => (
            <button
              key={k.kind}
              type="button"
              role="radio"
              aria-checked={kind === k.kind}
              onClick={() => setKind(k.kind)}
              className={cn(
                "rounded-xl border-[1.5px] px-3 py-2.5 text-left",
                kind === k.kind ? (k.kind === "BLACKLIST" ? "border-bad bg-bad-soft" : "border-accent bg-accent-soft/60") : "border-line hover:bg-surface-2",
              )}
              data-testid={`kind-${k.kind}`}
            >
              <span className="flex items-center gap-2 text-[13.5px] font-bold">
                {k.icon} {t(`vehicles.kind.${k.kind}`)}
              </span>
              <span className="mt-0.5 block text-[11.5px] font-semibold text-muted">{t(`vehicles.kindHint.${k.kind}`)}</span>
            </button>
          ))}
        </div>
      </div>

      {kind === "WHITELIST" && (
        <div>
          <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("vehicles.discount")}</div>
          <div className="flex flex-wrap gap-2">
            {[10, 25, 50, 100].map((d) => (
              <button key={d} type="button" onClick={() => setDiscount(d)} className={cn("rounded-full border px-3 py-1.5 font-mono text-[13px] font-bold", discount === d ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} aria-pressed={discount === d}>
                {d === 100 ? t("vehicles.free") : `−${d}%`}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="v-lot">{t("vehicles.lot")}</Label>
          <Select id="v-lot" value={lotId} onChange={(e) => setLotId(e.target.value)}>
            <option value="">{t("vehicles.allLots")}</option>
            {(lots.data ?? []).map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </Select>
        </div>
        {kind === "SUBSCRIPTION" && (
          <div>
            <Label htmlFor="v-price">{t("vehicles.price")}</Label>
            <Input id="v-price" type="number" min={0} step={1000} className="font-mono" value={price} onChange={(e) => setPrice(e.target.value)} data-testid="subscription-price" />
          </div>
        )}
      </div>
      <div>
        <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("vehicles.period")}</div>
        <div className="flex flex-wrap gap-2">
          {PERIODS.filter((p) => kind !== "SUBSCRIPTION" || p.months !== null).map((p) => (
            <button key={p.id} type="button" onClick={() => setPeriod(p.id)} className={cn("rounded-full border px-3 py-1.5 text-[12.5px] font-bold", (period === p.id || (kind === "SUBSCRIPTION" && p.months === effectiveMonths && months === null)) ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} aria-pressed={period === p.id} data-testid={`period-${p.id}`}>
              {t(`vehicles.periods.${p.id}`)}
            </button>
          ))}
        </div>
        <p className="mt-1 text-[12px] font-semibold text-muted">{until ? `${t("vehicles.until")} ${until.toLocaleDateString()}` : t("vehicles.foreverHint")}</p>
      </div>
      <div>
        <Label htmlFor="v-note">{t("vehicles.noteLabel")}</Label>
        <Input id="v-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t(`vehicles.notePh.${kind}`)} data-testid="vehicle-note" />
        {!noteOk && <p className="mt-1 text-[12px] font-semibold text-muted">{t("vehicles.noteHint")}</p>}
      </div>
      {(addRule.isError || addSub.isError) && <ErrorBox error={addRule.error ?? addSub.error} />}
      <div className="flex">
        <Button type="submit" size="lg" variant={kind === "BLACKLIST" ? "danger" : "default"} className="ml-auto" disabled={!canSubmit} data-testid="vehicle-submit">
          {kind === "BLACKLIST" ? t("vehicles.submitBlacklist") : t("vehicles.submit")}
        </Button>
      </div>
    </form>
  );
}
