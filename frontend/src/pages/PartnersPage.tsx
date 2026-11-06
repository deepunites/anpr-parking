import { useState, type FormEvent } from "react";
import { Download, Handshake, KeyRound, Plus } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useCreatePartnerMember, usePartnerMembers, usePartnerReport, usePartners, useSavePartner, type PartnerOut } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { Card, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Drawer } from "@/components/ui/drawer";
import { Input, Label } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/empty";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { formatClock, formatMinor } from "@/lib/format";

function thisMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** Партнёры площадки (MediaPark: компьютерный клуб): плата в месяц, входы барменов и расчёт за месяц —
    сколько машин клуб выпустил за свой счёт и сколько он должен площадке. */
export function PartnersPage() {
  const { t } = useTranslation();
  const { lotId } = useLotStore();
  const partners = usePartners();
  const [editing, setEditing] = useState<PartnerOut | "new" | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const list = (partners.data ?? []).filter((p) => !lotId || p.lot_id === lotId);
  const current = list.find((p) => p.id === selected) ?? list[0] ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("partners.title")}</h1>
        <Button className="ml-auto" onClick={() => setEditing("new")} disabled={!lotId} data-testid="partner-add">
          <Plus className="h-4 w-4" aria-hidden="true" /> {t("partners.add")}
        </Button>
      </div>
      {partners.isError && <ErrorBox error={partners.error} />}
      {partners.data && list.length === 0 && <EmptyState title={t("partners.empty")} hint={t("partners.emptyHint")} />}
      <div className="grid gap-3 sm:grid-cols-2">
        {list.map((p) => (
          <Card key={p.id} className={current?.id === p.id ? "border-accent" : undefined} data-testid="partner-card">
            <div className="flex items-start gap-3">
              <Handshake className="mt-1 h-5 w-5 text-accent" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <div className="text-[16px] font-extrabold">{p.name}</div>
                <div className="text-[13px] font-semibold text-muted">
                  {fmt(t("partners.fee"), { v: formatMinor(p.monthly_fee_minor) })}
                  {p.price_per_car_minor > 0 && ` + ${fmt(t("partners.price"), { v: formatMinor(p.price_per_car_minor) })}`}
                </div>
              </div>
              {!p.active && <Badge tone="gray">{t("partners.inactive")}</Badge>}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" variant={current?.id === p.id ? "default" : "outline"} onClick={() => setSelected(p.id)}>
                {t("partners.report")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setEditing(p)}>
                {t("partners.edit")}
              </Button>
            </div>
          </Card>
        ))}
      </div>
      {current && <Members partner={current} />}
      {current && <Report partner={current} />}
      <Drawer open={editing !== null} onClose={() => setEditing(null)} title={editing === "new" ? t("partners.add") : t("partners.edit")} width="w-[460px]">
        {editing !== null && lotId && <PartnerForm partner={editing === "new" ? null : editing} lotId={lotId} onDone={() => setEditing(null)} />}
      </Drawer>
    </div>
  );
}

function PartnerForm({ partner, lotId, onDone }: { partner: PartnerOut | null; lotId: string; onDone: () => void }) {
  const { t } = useTranslation();
  const save = useSavePartner();
  const [name, setName] = useState(partner?.name ?? "");
  const [fee, setFee] = useState(partner ? String(partner.monthly_fee_minor / 100) : "");
  const [price, setPrice] = useState(partner && partner.price_per_car_minor > 0 ? String(partner.price_per_car_minor / 100) : "");
  const [active, setActive] = useState(partner?.active ?? true);
  const toMinor = (v: string) => (v.trim() === "" ? 0 : Math.round(Number(v.replace(/\s/g, "").replace(",", ".")) * 100));
  const feeMinor = toMinor(fee);
  const priceMinor = toMinor(price);
  const valid = name.trim().length > 0 && fee.trim() !== "" && [feeMinor, priceMinor].every((v) => Number.isFinite(v) && v >= 0);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    save.mutate(
      { id: partner?.id, lotId, name: name.trim(), feeMinor, priceMinor, active, reason: partner ? "изменение партнёра" : "новый партнёр площадки" },
      {
        onSuccess: () => {
          toast.ok(t("partners.saved"));
          onDone();
        },
      },
    );
  };
  return (
    <form className="space-y-3" onSubmit={submit}>
      <div>
        <Label htmlFor="partner-name">{t("partners.name")}</Label>
        <Input id="partner-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("partners.namePh")} data-testid="partner-name" />
      </div>
      <div>
        <Label htmlFor="partner-fee">{t("partners.feeLabel")}</Label>
        <Input id="partner-fee" inputMode="decimal" className="font-mono" value={fee} onChange={(e) => setFee(e.target.value)} placeholder="500000" data-testid="partner-fee" />
        <p className="mt-1 text-[12px] font-semibold text-muted">{t("partners.feeHint")}</p>
      </div>
      <div>
        <Label htmlFor="partner-price">{t("partners.priceLabel")}</Label>
        <Input id="partner-price" inputMode="decimal" className="font-mono" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="0" data-testid="partner-price" />
        <p className="mt-1 text-[12px] font-semibold text-muted">{t("partners.priceHint")}</p>
      </div>
      {partner && (
        <label className="flex items-center gap-2 text-[14px] font-bold">
          <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> {t("partners.active")}
        </label>
      )}
      <ErrorBox error={save.error} />
      <Button type="submit" size="lg" disabled={!valid || save.isPending} data-testid="partner-save">
        {t("session.save")}
      </Button>
    </form>
  );
}

function Members({ partner }: { partner: PartnerOut }) {
  const { t } = useTranslation();
  const members = usePartnerMembers(partner.id);
  const create = useCreatePartnerMember(partner.id);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const email = login.includes("@") ? login.trim() : null;
  const phone = email ? null : login.trim() || null;
  const valid = name.trim().length > 0 && login.trim().length >= 5 && password.length >= 12;
  return (
    <Card>
      <CardTitle
        right={
          <Button size="sm" variant="outline" onClick={() => setAdding((v) => !v)} data-testid="partner-member-add">
            <KeyRound className="h-4 w-4" aria-hidden="true" /> {t("partners.addLogin")}
          </Button>
        }
      >
        {fmt(t("partners.logins"), { name: partner.name })}
      </CardTitle>
      {members.data?.length === 0 && <p className="text-[13px] font-semibold text-muted">{t("partners.noLogins")}</p>}
      <ul className="space-y-1">
        {members.data?.map((m) => (
          <li key={m.user_id} className="flex items-center gap-2 text-[14px] font-semibold">
            {m.display_name} <span className="text-muted">{m.login}</span>
            {!m.active && <Badge tone="gray">{t("partners.inactive")}</Badge>}
          </li>
        ))}
      </ul>
      {adding && (
        <form
          className="mt-3 grid gap-2 sm:grid-cols-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!valid) return;
            create.mutate(
              { email, phone, display_name: name.trim(), password, reason: `вход бармена партнёра «${partner.name}»` },
              {
                onSuccess: () => {
                  toast.ok(t("partners.loginCreated"));
                  setAdding(false);
                  setName("");
                  setLogin("");
                  setPassword("");
                },
              },
            );
          }}
        >
          <Input aria-label={t("partners.loginName")} placeholder={t("partners.loginName")} value={name} onChange={(e) => setName(e.target.value)} />
          <Input aria-label={t("partners.login")} placeholder={t("partners.login")} value={login} onChange={(e) => setLogin(e.target.value)} />
          <Input aria-label={t("partners.password")} placeholder={t("partners.password")} type="text" value={password} onChange={(e) => setPassword(e.target.value)} />
          <p className="text-[12px] font-semibold text-muted sm:col-span-3">{t("partners.loginHint")}</p>
          <ErrorBox error={create.error} />
          <Button type="submit" disabled={!valid || create.isPending} className="sm:col-span-3">
            {t("partners.createLogin")}
          </Button>
        </form>
      )}
    </Card>
  );
}

function Report({ partner }: { partner: PartnerOut }) {
  const { t } = useTranslation();
  const [month, setMonth] = useState(thisMonth());
  const report = usePartnerReport(partner.id, month);
  const csv = () => {
    if (!report.data) return;
    const rows = [["Дата отметки", "Номер", "Въезд", "Выезд", "Статус", "Кто отметил"]];
    for (const v of report.data.items) rows.push([v.created_at, v.plate ?? "", v.entry_time, v.exit_time ?? "", t(`partner.state.${v.state}`), v.created_by ?? ""]);
    rows.push([], ["Машин за месяц", String(report.data.count)], ["Плата в месяц, сум", String(report.data.partner.monthly_fee_minor / 100)]);
    if (report.data.cars_total_minor > 0) rows.push(["За машины, сум", String(report.data.cars_total_minor / 100)]);
    rows.push(["Итого к оплате, сум", String(report.data.total_minor / 100)]);
    const blob = new Blob(["﻿" + rows.map((r) => r.map((c) => `"${c.replace(/"/g, '""')}"`).join(";")).join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${partner.name}-${month}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <Card data-testid="partner-report">
      <CardTitle
        right={
          <div className="flex items-center gap-2">
            <Input type="month" aria-label={t("partners.month")} value={month} onChange={(e) => e.target.value && setMonth(e.target.value)} className="w-40" />
            <Button size="sm" variant="outline" onClick={csv} disabled={!report.data}>
              <Download className="h-4 w-4" aria-hidden="true" /> CSV
            </Button>
          </div>
        }
      >
        {fmt(t("partners.reportTitle"), { name: partner.name })}
      </CardTitle>
      {report.isError && <ErrorBox error={report.error} />}
      {report.data && (
        <>
          <div className="mb-3 flex flex-wrap gap-6">
            <div>
              <div className="text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("partners.cars")}</div>
              <div className="text-2xl font-extrabold" data-testid="partner-report-count">{report.data.count}</div>
            </div>
            <div>
              <div className="text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("partners.feeShort")}</div>
              <div className="font-mono text-2xl font-extrabold">{formatMinor(report.data.partner.monthly_fee_minor)}</div>
            </div>
            {report.data.cars_total_minor > 0 && (
              <div>
                <div className="text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("partners.forCars")}</div>
                <div className="font-mono text-2xl font-extrabold">{formatMinor(report.data.cars_total_minor)}</div>
              </div>
            )}
            <div>
              <div className="text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("partners.due")}</div>
              <div className="font-mono text-2xl font-extrabold text-ok-ink" data-testid="partner-report-total">{formatMinor(report.data.total_minor)}</div>
            </div>
          </div>
          <Table>
            <thead>
              <tr>
                <Th>{t("partners.when")}</Th>
                <Th>{t("session.plate")}</Th>
                <Th>{t("session.entry")}</Th>
                <Th>{t("session.exitTime")}</Th>
                <Th>{t("partners.status")}</Th>
                <Th>{t("partners.who")}</Th>
              </tr>
            </thead>
            <tbody>
              {report.data.items.map((v) => (
                <tr key={v.id}>
                  <Td mono>{formatClock(v.created_at, undefined)}</Td>
                  <Td>
                    <Plate plate={v.plate} size="sm" />
                  </Td>
                  <Td mono>{formatClock(v.entry_time, undefined)}</Td>
                  <Td mono>{v.exit_time ? formatClock(v.exit_time, undefined) : "—"}</Td>
                  <Td>{t(`partner.state.${v.state}`)}</Td>
                  <Td className="text-[12.5px]">{v.created_by ?? ""}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </>
      )}
    </Card>
  );
}
