import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Camera, Check, Copy, KeyRound, Map as MapIcon, ParkingSquare, PartyPopper, Users, Wallet } from "lucide-react";
import { useLayout } from "@/api/hooks";
import { normalize } from "@/lib/layout";
import { LayoutCanvas } from "@/components/map/LayoutCanvas";
import { useQuickLayout } from "@/pages/MapPage";
import { fmt, useTranslation } from "@/i18n";
import { useCameras, useCreateCamera, useCreateLot, useIssueCameraKey, useLots, useQuickTariff, useTariffPlans, type IssuedKeyOut } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { cn } from "@/lib/cn";
import { InviteForm, InviteLinkView, type IssuedInvite } from "@/components/staff/InviteForm";
import { copyToClipboard } from "@/lib/clipboard";

type StepId = "lot" | "tariff" | "cameras" | "map" | "team" | "done";
const STEPS: { id: StepId; icon: ReactNode }[] = [
  { id: "lot", icon: <ParkingSquare className="h-4 w-4" aria-hidden="true" /> },
  { id: "tariff", icon: <Wallet className="h-4 w-4" aria-hidden="true" /> },
  { id: "cameras", icon: <Camera className="h-4 w-4" aria-hidden="true" /> },
  { id: "map", icon: <MapIcon className="h-4 w-4" aria-hidden="true" /> },
  { id: "team", icon: <Users className="h-4 w-4" aria-hidden="true" /> },
];

/** Мастер «Быстрый старт» после регистрации: площадка → цена стоянки → камеры (с ключами) → сотрудники.
    Готовность шагов определяется по данным (площадки, тарифы, камеры), поэтому мастер можно открыть в любой момент. */
export function WelcomePage() {
  const { t } = useTranslation();
  const lots = useLots();
  const cameras = useCameras();
  const { lotId, setLotId } = useLotStore();
  const lot = (lots.data ?? []).find((l) => l.id === lotId) ?? lots.data?.[0] ?? null;
  const tariffs = useTariffPlans(lot?.id ?? null);
  const lotCameras = (cameras.data ?? []).filter((c) => c.lot_id === lot?.id && c.active);
  const layout = useLayout(lot?.id ?? null);
  const done: Record<StepId, boolean> = {
    lot: lot !== null,
    tariff: (tariffs.data ?? []).length > 0,
    cameras: lotCameras.length > 0,
    map: (layout.data?.version ?? 0) > 0,
    team: false,
    done: false,
  };
  const [step, setStep] = useState<StepId | null>(null);
  const firstOpen = STEPS.find((s) => !done[s.id])?.id ?? "team";
  const current = step ?? firstOpen;

  if (lots.isPending) return <Skeleton className="mt-4 h-64 w-full max-w-3xl" />;

  return (
    <div className="mx-auto max-w-3xl py-2" data-testid="welcome">
      <h1 className="text-[22px] font-extrabold">{t("wizard.title")}</h1>
      <p className="mb-4 text-[13.5px] font-semibold text-muted">{t("wizard.hint")}</p>
      <ol className="mb-4 grid grid-cols-5 gap-2" aria-label={t("wizard.title")}>
        {STEPS.map((s, i) => (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => setStep(s.id)}
              aria-current={current === s.id ? "step" : undefined}
              disabled={s.id !== "lot" && !done.lot}
              className={cn(
                "flex w-full items-center gap-2 rounded-xl border px-2.5 py-2 text-left text-[12.5px] font-bold transition-colors disabled:opacity-50",
                current === s.id ? "border-accent bg-accent-soft text-accent-ink" : "border-line bg-surface",
              )}
              data-testid={`wizard-step-${s.id}`}
            >
              <span className={cn("grid h-6 w-6 shrink-0 place-items-center rounded-full text-[11px]", done[s.id] ? "bg-ok text-white" : "bg-surface-2")}>
                {done[s.id] ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : i + 1}
              </span>
              <span className="hidden truncate sm:inline">{t(`wizard.${s.id}`)}</span>
              <span className="sm:hidden">{s.icon}</span>
            </button>
          </li>
        ))}
      </ol>
      <Card className="p-5">
        {current === "lot" && (
          <LotStep
            lot={lot ? { name: lot.name, capacity: lot.capacity } : null}
            onDone={(id) => {
              if (id) setLotId(id);
              setStep("tariff");
            }}
          />
        )}
        {current === "tariff" && lot && <TariffStep lotId={lot.id} configured={done.tariff} onDone={() => setStep("cameras")} />}
        {current === "cameras" && lot && <CamerasStep lotId={lot.id} existingCodes={(cameras.data ?? []).map((c) => c.code)} connected={lotCameras.length} onDone={() => setStep("map")} />}
        {current === "map" && lot && <MapStep lot={lot} onDone={() => setStep("team")} />}
        {current === "team" && <TeamStep onDone={() => setStep("done")} />}
        {current === "done" && <DoneStep />}
      </Card>
    </div>
  );
}

function StepHead({ icon, title, hint }: { icon: ReactNode; title: string; hint: string }) {
  return (
    <div className="mb-4 flex items-start gap-3">
      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent-soft text-accent-ink">{icon}</span>
      <div>
        <h2 className="text-[17px] font-extrabold">{title}</h2>
        <p className="text-[13px] font-semibold text-muted">{hint}</p>
      </div>
    </div>
  );
}

function LotStep({ lot, onDone }: { lot: { name: string; capacity: number } | null; onDone: (lotId: string | null) => void }) {
  const { t } = useTranslation();
  const create = useCreateLot();
  const [name, setName] = useState("");
  const [capacity, setCapacity] = useState("50");
  const [address, setAddress] = useState("");
  const cap = Number(capacity);
  const valid = name.trim().length >= 1 && Number.isInteger(cap) && cap > 0;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || create.isPending) return;
    create.mutate(
      { code: `LOT-${Date.now().toString(36).toUpperCase().slice(-5)}`, name: name.trim(), capacity: cap, timezone: "Asia/Tashkent", address: address.trim() || null, reason: t("wizard.lotReason") },
      { onSuccess: (l) => onDone(l.id) },
    );
  };
  return (
    <div>
      <StepHead icon={<ParkingSquare className="h-5 w-5" aria-hidden="true" />} title={t("wizard.lot")} hint={t("wizard.lotHint")} />
      {lot ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex items-center gap-2 text-[15px] font-bold text-ok-ink" data-testid="wizard-lot-done">
            <Check className="h-5 w-5" aria-hidden="true" /> {fmt(t("wizard.lotDone"), { name: lot.name, n: lot.capacity })}
          </span>
          <Button className="ml-auto" onClick={() => onDone(null)}>
            {t("wizard.next")}
          </Button>
        </div>
      ) : (
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_140px]">
          <div>
            <Label htmlFor="wz-lot-name">{t("common.name")}</Label>
            <Input id="wz-lot-name" autoFocus required placeholder={t("lots.namePh")} value={name} onChange={(e) => setName(e.target.value)} data-testid="wz-lot-name" />
          </div>
          <div>
            <Label htmlFor="wz-lot-cap">{t("lots.capacity")}</Label>
            <Input id="wz-lot-cap" type="number" min={1} inputMode="numeric" required value={capacity} onChange={(e) => setCapacity(e.target.value)} data-testid="wz-lot-capacity" />
          </div>
          <div className="sm:col-span-2">
            <Label htmlFor="wz-lot-addr">{t("lots.address")}</Label>
            <Input id="wz-lot-addr" placeholder={t("lots.addressPh")} value={address} onChange={(e) => setAddress(e.target.value)} />
          </div>
          {create.isError && (
            <div className="sm:col-span-2">
              <ErrorBox error={create.error} />
            </div>
          )}
          <div className="flex sm:col-span-2">
            <Button type="submit" size="lg" className="ml-auto" disabled={!valid || create.isPending} data-testid="wz-lot-save">
              {t("wizard.save")}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}

function TariffStep({ lotId, configured, onDone }: { lotId: string; configured: boolean; onDone: () => void }) {
  const { t } = useTranslation();
  const save = useQuickTariff();
  const [firstHour, setFirstHour] = useState("5000");
  const [perHour, setPerHour] = useState("3000");
  const [grace, setGrace] = useState("15");
  const [dailyMax, setDailyMax] = useState("");
  const nums = [Number(firstHour), Number(perHour), Number(grace)];
  const valid = nums.every((n) => Number.isFinite(n) && n >= 0) && (dailyMax === "" || Number(dailyMax) > 0);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || save.isPending) return;
    save.mutate(
      { lotId, firstHour: Number(firstHour), perHour: Number(perHour), graceMinutes: Number(grace), dailyMax: dailyMax === "" ? null : Number(dailyMax) },
      { onSuccess: onDone },
    );
  };
  return (
    <div>
      <StepHead icon={<Wallet className="h-5 w-5" aria-hidden="true" />} title={t("wizard.tariff")} hint={t("wizard.tariffHint")} />
      {configured ? (
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-2 text-[15px] font-bold text-ok-ink" data-testid="wizard-tariff-done">
            <Check className="h-5 w-5" aria-hidden="true" /> {t("wizard.tariffDone")}
          </span>
          <Button className="ml-auto" onClick={onDone}>
            {t("wizard.next")}
          </Button>
        </div>
      ) : (
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2">
          <NumberField id="wz-first" label={t("wizard.firstHour")} value={firstHour} onChange={setFirstHour} step={500} />
          <NumberField id="wz-per" label={t("wizard.perHour")} value={perHour} onChange={setPerHour} step={500} />
          <NumberField id="wz-grace" label={t("wizard.graceMin")} value={grace} onChange={setGrace} step={5} />
          <NumberField id="wz-daily" label={t("wizard.dailyMax")} value={dailyMax} onChange={setDailyMax} step={5000} placeholder={t("wizard.dailyMaxPh")} />
          {save.isError && (
            <div className="sm:col-span-2">
              <ErrorBox error={save.error} />
            </div>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <Button variant="ghost" onClick={onDone}>
              {t("wizard.skip")}
            </Button>
            <Button type="submit" size="lg" className="ml-auto" disabled={!valid || save.isPending} data-testid="wz-tariff-save">
              {t("wizard.save")}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}

function NumberField({ id, label, value, onChange, step, placeholder }: { id: string; label: string; value: string; onChange: (v: string) => void; step: number; placeholder?: string }) {
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} type="number" min={0} step={step} inputMode="numeric" className="font-mono" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

function nextCode(prefix: string, used: Set<string>): string {
  for (let i = 1; i < 100; i++) {
    const code = `${prefix}${String(i).padStart(2, "0")}`;
    if (!used.has(code)) {
      used.add(code);
      return code;
    }
  }
  return `${prefix}${Date.now() % 1000}`;
}

function CamerasStep({ lotId, existingCodes, connected, onDone }: { lotId: string; existingCodes: string[]; connected: number; onDone: () => void }) {
  const { t } = useTranslation();
  const create = useCreateCamera();
  const issue = useIssueCameraKey();
  const [entry, setEntry] = useState(true);
  const [exit, setExit] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [issued, setIssued] = useState<{ name: string; key: IssuedKeyOut }[]>([]);

  const add = async () => {
    setBusy(true);
    setError(null);
    const used = new Set(existingCodes);
    const out: { name: string; key: IssuedKeyOut }[] = [];
    try {
      for (const [on, direction, name] of [
        [entry, "IN", t("wizard.camIn")],
        [exit, "OUT", t("wizard.camOut")],
      ] as const) {
        if (!on) continue;
        const cam = await create.mutateAsync({
          lot_id: lotId,
          code: nextCode(direction === "IN" ? "ENTRY_" : "EXIT_", used),
          name,
          direction,
          vendor: "GENERIC",
          confidence_min: 80,
          confidence_review_min: 60,
          max_clock_skew_seconds: 120,
          heartbeat_interval_sec: 300,
          reason: t("wizard.reason"),
        });
        out.push({ name, key: await issue.mutateAsync({ cameraId: cam.id, label: name, reason: t("wizard.reason") }) });
      }
    } catch (err) {
      setError(err);
    } finally {
      setIssued((prev) => [...prev, ...out]);
      setBusy(false);
    }
  };

  return (
    <div>
      <StepHead icon={<Camera className="h-5 w-5" aria-hidden="true" />} title={t("wizard.cameras")} hint={t("wizard.camerasHint")} />
      {issued.length > 0 ? (
        <KeysMemo issued={issued} onDone={onDone} />
      ) : (
        <div className="space-y-3">
          {connected > 0 && (
            <p className="flex items-center gap-2 text-[14px] font-bold text-ok-ink" data-testid="wizard-cameras-done">
              <Check className="h-5 w-5" aria-hidden="true" /> {fmt(t("wizard.camerasDone"), { n: connected })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {(
              [
                [entry, setEntry, t("wizard.camIn"), "wz-cam-in"],
                [exit, setExit, t("wizard.camOut"), "wz-cam-out"],
              ] as const
            ).map(([on, set, label, testId]) => (
              <label key={testId} className={cn("flex cursor-pointer items-center gap-2 rounded-xl border-2 px-3 py-2.5 text-[14px] font-bold", on ? "border-accent bg-accent-soft text-accent-ink" : "border-line")}>
                <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]" checked={on} onChange={(e) => set(e.target.checked)} data-testid={testId} />
                {label}
              </label>
            ))}
          </div>
          {error !== null && <ErrorBox error={error} />}
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onDone}>
              {connected > 0 ? t("wizard.next") : t("wizard.skip")}
            </Button>
            <Button size="lg" className="ml-auto" disabled={(!entry && !exit) || busy} onClick={() => void add()} data-testid="wz-cameras-add">
              {t("wizard.addCameras")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function KeysMemo({ issued, onDone }: { issued: { name: string; key: IssuedKeyOut }[]; onDone: () => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState<string | null>(null);
  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(null), 1500);
    return () => clearTimeout(id);
  }, [copied]);
  return (
    <div className="space-y-3" data-testid="wizard-keys">
      <div className="rounded-xl border border-warn bg-warn-soft p-3 text-warn-ink">
        <div className="flex items-center gap-2 text-[14px] font-extrabold">
          <KeyRound className="h-4 w-4" aria-hidden="true" /> {t("wizard.keysTitle")}
        </div>
        <p className="text-[12.5px] font-semibold">{t("wizard.keysHint")}</p>
      </div>
      {issued.map(({ name, key }) => (
        <div key={key.id} className="flex flex-wrap items-center gap-2 rounded-xl border border-line p-3">
          <span className="text-[13.5px] font-bold">{name}</span>
          <code className="min-w-0 flex-1 break-all rounded-lg bg-surface-2 px-2 py-1 font-mono text-[12.5px]">{key.key}</code>
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              try {
                await copyToClipboard(key.key);
                setCopied(key.id);
              } catch {
                /* буфер недоступен — ключ виден текстом */
              }
            }}
          >
            <Copy className="h-3.5 w-3.5" aria-hidden="true" /> {copied === key.id ? t("wizard.copied") : t("wizard.copy")}
          </Button>
        </div>
      ))}
      <div className="flex">
        <Button size="lg" className="ml-auto" onClick={onDone} data-testid="wz-keys-next">
          {t("wizard.next")}
        </Button>
      </div>
    </div>
  );
}

function MapStep({ lot, onDone }: { lot: { id: string; name: string; capacity: number }; onDone: () => void }) {
  const { t } = useTranslation();
  const layout = useLayout(lot.id);
  const quick = useQuickLayout(lot.id, lot.capacity, lot.name);
  const has = (layout.data?.version ?? 0) > 0;
  return (
    <div>
      <StepHead icon={<MapIcon className="h-5 w-5" aria-hidden="true" />} title={t("wizard.map")} hint={t("map.wizardHint")} />
      {has && layout.data && (
        <div className="mb-3 rounded-xl border border-line p-2" data-testid="wizard-map-preview">
          <LayoutCanvas doc={normalize(layout.data.document)} mode="view" />
        </div>
      )}
      {quick.error !== null && <ErrorBox error={quick.error} />}
      <div className="flex flex-wrap items-center gap-2">
        {!has && (
          <Button size="lg" onClick={() => quick.run()} disabled={quick.pending} data-testid="wz-map-quick">
            <MapIcon className="h-4 w-4" aria-hidden="true" /> {t("map.quick")}
          </Button>
        )}
        <Link to="/map?tab=editor" className="text-[13.5px] font-bold text-accent-ink underline">
          {t("map.openEditor")}
        </Link>
        <Button size="lg" variant={has ? "default" : "ghost"} className="ml-auto" onClick={onDone} data-testid="wz-map-next">
          {has ? t("wizard.next") : t("wizard.skip")}
        </Button>
      </div>
    </div>
  );
}

function TeamStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const [issued, setIssued] = useState<IssuedInvite | null>(null);
  const [count, setCount] = useState(0);
  return (
    <div>
      <StepHead icon={<Users className="h-5 w-5" aria-hidden="true" />} title={t("wizard.team")} hint={t("wizard.teamHint")} />
      {issued ? (
        <InviteLinkView issued={issued} onDone={() => setIssued(null)} doneLabel={t("wizard.inviteMore")} />
      ) : (
        <InviteForm
          key={count}
          compact
          roles={["OPERATOR", "CASHIER", "MANAGER"]}
          onIssued={(i) => {
            setIssued(i);
            setCount((c) => c + 1);
          }}
        />
      )}
      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <span className="text-[12.5px] font-semibold text-muted">{count > 0 ? `${t("users.invites")}: ${count}` : t("wizard.teamLater")}</span>
        <Button size="lg" variant={count > 0 ? "default" : "ghost"} className="ml-auto" onClick={onDone} data-testid="wz-team-next">
          {count > 0 ? t("wizard.next") : t("wizard.skip")}
        </Button>
      </div>
    </div>
  );
}

function DoneStep() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="py-4 text-center" data-testid="wizard-done">
      <PartyPopper className="mx-auto mb-2 h-10 w-10 text-accent" aria-hidden="true" />
      <h2 className="text-[19px] font-extrabold">{t("wizard.finishTitle")}</h2>
      <p className="mx-auto mb-4 max-w-md text-[13.5px] font-semibold text-muted">{t("wizard.finishHint")}</p>
      <Button size="lg" onClick={() => navigate("/live")} data-testid="wz-go-live">
        {t("wizard.goLive")}
      </Button>
    </div>
  );
}
