import { useEffect, useState } from "react";
import { Pencil, Plus } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import { useCreateLot, useLots, useUpdateLot, type LotOut, type PoliciesModel } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { Card, Eyebrow } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Segmented, Select } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { StatusDot } from "@/components/ui/badge";
import { Drawer } from "@/components/ui/drawer";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";

const TIMEZONES = ["Asia/Tashkent", "Asia/Samarkand", "Asia/Almaty", "Europe/Moscow"];

const DEFAULT_POLICIES: PoliciesModel = {
  deny_entry_when_full: false,
  unknown_exit_policy: "MANUAL_REVIEW",
  exit_grace_minutes: 15,
  exit_inferred_after_minutes: 60,
  late_event_window_hours: 24,
  max_session_hours: 72,
  reentry_window_minutes: 5,
  barrier_cidr_allowlist: [],
};

function suggestCode(name: string, existing: LotOut[]): string {
  const base =
    name
      .toUpperCase()
      .replace(/[^A-Z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 12) || "LOT";
  const used = new Set(existing.map((l) => l.code));
  if (!used.has(base)) return base;
  for (let i = 2; i < 100; i++) if (!used.has(`${base}-${i}`)) return `${base}-${i}`;
  return `${base}-${Date.now() % 1000}`;
}

/* Площадки клиента: список, создание и редактирование — вместимость (число мест), адрес, режимы (что делать при
   заполнении, выезде без въезда, обрыве), разрешённые подсети контроллеров. Права: lots:write (администратор, разработчик). */
export function LotsPage() {
  const { t } = useTranslation();
  const { me } = useAuth();
  const canWrite = hasPermission(me, "lots:write");
  const lots = useLots();
  const { setLotId } = useLotStore();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<LotOut | null>(null);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.lots")}</h1>
        {canWrite && (
          <Button className="ml-auto" onClick={() => setAdding(true)} data-testid="add-lot">
            <Plus className="h-4 w-4" aria-hidden="true" /> {t("lots.add")}
          </Button>
        )}
      </div>
      <Card>
        {lots.isPending && <SkeletonRows />}
        {lots.isError && <ErrorBox error={lots.error} onRetry={() => void lots.refetch()} />}
        {lots.data && lots.data.length === 0 && <EmptyState title={t("lots.empty")} hint={t("lots.emptyHint")} />}
        {lots.data && lots.data.length > 0 && (
          <Table data-testid="lots-table">
            <thead>
              <tr>
                <Th>{t("lots.lot")}</Th>
                <Th>{t("common.code")}</Th>
                <Th className="text-right">{t("lots.capacity")}</Th>
                <Th>{t("lots.whenFull")}</Th>
                <Th>{t("lots.unknownExit")}</Th>
                <Th>{t("session.status")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {lots.data.map((l) => (
                <tr key={l.id} data-testid="lot-row" data-code={l.code}>
                  <Td>
                    <b>{l.name}</b>
                    <span className="block text-[12px] font-semibold text-muted">
                      {l.address || "—"} · {l.timezone}
                    </span>
                  </Td>
                  <Td mono>{l.code}</Td>
                  <Td mono className="text-right">
                    {l.capacity}
                  </Td>
                  <Td className="text-[12.5px]">{l.policies.deny_entry_when_full ? t("lots.deny") : t("lots.allow")}</Td>
                  <Td className="text-[12.5px]">{l.policies.unknown_exit_policy === "OPEN_FREE" ? t("lots.openFree") : t("lots.toReview")}</Td>
                  <Td>
                    <StatusDot tone={l.active ? "green" : "red"}>{l.active ? t("common.active") : t("common.inactive")}</StatusDot>
                  </Td>
                  <Td>
                    {canWrite && (
                      <Button variant="outline" size="sm" onClick={() => setEditing(l)} data-testid="edit-lot">
                        <Pencil className="h-4 w-4" aria-hidden="true" /> {t("lots.edit")}
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <Drawer open={adding} onClose={() => setAdding(false)} title={t("lots.new")} width="w-[520px]">
        <LotForm
          existing={lots.data ?? []}
          onDone={(lot) => {
            setAdding(false);
            if (lot) setLotId(lot.id);
          }}
        />
      </Drawer>
      <Drawer open={editing !== null} onClose={() => setEditing(null)} title={editing?.name ?? ""} width="w-[520px]">
        {editing && <LotForm existing={lots.data ?? []} lot={editing} onDone={() => setEditing(null)} />}
      </Drawer>
    </div>
  );
}

function LotForm({ lot, existing, onDone }: { lot?: LotOut; existing: LotOut[]; onDone: (lot: LotOut | null) => void }) {
  const { t } = useTranslation();
  const create = useCreateLot();
  const update = useUpdateLot();
  const [name, setName] = useState(lot?.name ?? "");
  const [code, setCode] = useState(lot?.code ?? "");
  const [address, setAddress] = useState(lot?.address ?? "");
  const [capacity, setCapacity] = useState(String(lot?.capacity ?? 100));
  const [timezone, setTimezone] = useState(lot?.timezone ?? "Asia/Tashkent");
  const [policies, setPolicies] = useState<PoliciesModel>(lot?.policies ?? DEFAULT_POLICIES);
  const [cidrs, setCidrs] = useState((lot?.policies.barrier_cidr_allowlist ?? []).join(", "));
  const [reason, setReason] = useState("");
  useEffect(() => {
    if (!lot && !code) setCode(suggestCode(name, existing));
  }, [name, lot, code, existing]);
  const cap = Number(capacity);
  const cidrList = cidrs
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const cidrBad = cidrList.find((c) => !/^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/.test(c));
  const valid = name.trim().length >= 2 && Number.isInteger(cap) && cap > 0 && (lot ? reason.trim().length >= 3 : true) && !cidrBad;
  const busy = create.isPending || update.isPending;
  const submit = () => {
    const pol: PoliciesModel = { ...policies, barrier_cidr_allowlist: cidrList };
    if (lot) {
      update.mutate(
        { lotId: lot.id, body: { name: name.trim(), address: address.trim() || null, capacity: cap, policies: pol, reason: reason.trim() } },
        {
          onSuccess: (l) => {
            toast.ok(`${t("lots.saved")} · ${l.name}`);
            onDone(l);
          },
        },
      );
    } else {
      create.mutate(
        { name: name.trim(), code: code.trim().toUpperCase() || suggestCode(name, existing), address: address.trim() || null, capacity: cap, timezone, policies: pol, reason: t("lots.createReason") },
        {
          onSuccess: (l) => {
            toast.ok(`${t("lots.created")} · ${l.name}`);
            onDone(l);
          },
        },
      );
    }
  };
  const set = <K extends keyof PoliciesModel>(k: K, v: PoliciesModel[K]) => setPolicies((p) => ({ ...p, [k]: v }));

  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) submit();
      }}
      data-testid="lot-form"
    >
      <Label htmlFor="lot-name">{t("common.name")}</Label>
      <Input id="lot-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("lots.namePh")} data-testid="lot-name" autoFocus />
      {!lot && (
        <>
          <Label htmlFor="lot-code" className="mt-3">
            {t("common.code")}
          </Label>
          <Input id="lot-code" className="font-mono" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} data-testid="lot-code" />
          <p className="text-[12px] font-semibold text-muted">{t("lots.codeHint")}</p>
        </>
      )}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label htmlFor="lot-capacity" className="mt-3">
            {t("lots.capacity")}
          </Label>
          <Input id="lot-capacity" type="number" min={1} className="font-mono text-lg" value={capacity} onChange={(e) => setCapacity(e.target.value)} data-testid="lot-capacity" />
        </div>
        <div>
          <Label htmlFor="lot-tz" className="mt-3">
            {t("lots.timezone")}
          </Label>
          <Select id="lot-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)} disabled={!!lot}>
            {TIMEZONES.map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <Label htmlFor="lot-address" className="mt-3">
        {t("lots.address")}
      </Label>
      <Input id="lot-address" value={address} onChange={(e) => setAddress(e.target.value)} placeholder={t("lots.addressPh")} />

      <Eyebrow className="mt-4 mb-1">{t("lots.whenFull")}</Eyebrow>
      <Segmented
        value={policies.deny_entry_when_full ? "deny" : "allow"}
        onChange={(v) => set("deny_entry_when_full", v === "deny")}
        ariaLabel={t("lots.whenFull")}
        options={[
          { value: "allow", label: t("lots.allow") },
          { value: "deny", label: t("lots.deny") },
        ]}
      />
      <p className="text-[12px] font-semibold text-muted">{t("lots.whenFullHint")}</p>
      <Eyebrow className="mt-3 mb-1">{t("lots.unknownExit")}</Eyebrow>
      <Segmented
        value={policies.unknown_exit_policy}
        onChange={(v) => set("unknown_exit_policy", v)}
        ariaLabel={t("lots.unknownExit")}
        options={[
          { value: "MANUAL_REVIEW", label: t("lots.toReview") },
          { value: "OPEN_FREE", label: t("lots.openFree") },
        ]}
      />
      <p className="text-[12px] font-semibold text-muted">{t("lots.unknownExitHint")}</p>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <div>
          <Label htmlFor="p-grace">{t("lots.grace")}</Label>
          <Input id="p-grace" type="number" min={0} max={240} value={policies.exit_grace_minutes} onChange={(e) => set("exit_grace_minutes", Number(e.target.value))} />
        </div>
        <div>
          <Label htmlFor="p-late">{t("lots.lateWindow")}</Label>
          <Input id="p-late" type="number" min={1} max={168} value={policies.late_event_window_hours} onChange={(e) => set("late_event_window_hours", Number(e.target.value))} />
        </div>
        <div>
          <Label htmlFor="p-max">{t("lots.maxSession")}</Label>
          <Input id="p-max" type="number" min={1} max={720} value={policies.max_session_hours} onChange={(e) => set("max_session_hours", Number(e.target.value))} />
        </div>
      </div>
      <Label htmlFor="lot-cidrs" className="mt-3">
        {t("lots.cidrs")}
      </Label>
      <Input id="lot-cidrs" className="font-mono" value={cidrs} onChange={(e) => setCidrs(e.target.value)} placeholder="10.100.5.0/24, 192.168.10.0/24" data-testid="lot-cidrs" />
      <p className={`text-[12px] font-semibold ${cidrBad ? "text-bad-ink" : "text-muted"}`}>{cidrBad ? `${t("lots.cidrBad")}: ${cidrBad}` : t("lots.cidrsHint")}</p>
      {lot && (
        <>
          <Label htmlFor="lot-reason" className="mt-3">
            {t("session.reason")}
          </Label>
          <Input id="lot-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("users.reasonPh")} data-testid="lot-reason" />
        </>
      )}
      {(create.isError || update.isError) && <ErrorBox error={create.error ?? update.error} />}
      <div className="mt-4 flex">
        <Button type="submit" className="ml-auto" disabled={!valid || busy} data-testid="lot-submit">
          {lot ? t("session.save") : t("lots.create")}
        </Button>
      </div>
    </form>
  );
}
