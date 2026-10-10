import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Copy, KeyRound, Pencil, Plug, Plus, Power, Zap } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import {
  useBarriers,
  useCameraKeys,
  useCameras,
  useCreateBarrier,
  useCreateCamera,
  useIssueCameraKey,
  useLots,
  useProbeBarrier,
  useTestOpenBarrier,
  useRevokeCameraKey,
  useUpdateBarrier,
  useUpdateCamera,
  type BarrierOut,
  type CameraOut,
  type IssuedKeyOut,
} from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import { Card, Eyebrow } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Segmented, Select } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { StatusDot, toneFor } from "@/components/ui/badge";
import { Drawer } from "@/components/ui/drawer";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { useReasonDialog } from "@/components/ReasonDialog";
import { toast } from "@/components/ui/toast";
import { Tabs } from "@/components/ui/tabs";
import { copyToClipboard } from "@/lib/clipboard";

type Tab = "cameras" | "barriers";

/* Устройства: камеры и стрелы на одной странице. Добавление — боковой панелью; ключ камеры показывается
   один раз в диалоге (скопировать / скачать памятку монтажнику); стрелу можно проверить кнопкой «Тест: открыть». */
export function DevicesPage() {
  const { t, i18n } = useTranslation();
  const { me } = useAuth();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "barriers" ? "barriers" : "cameras";
  const cameras = useCameras();
  const barriers = useBarriers();
  const lots = useLots();
  const { lotId } = useLotStore();
  const lot = (lots.data ?? []).find((l) => l.id === lotId);
  const timeZone = lot?.timezone;
  const canWrite = hasPermission(me, tab === "cameras" ? "cameras:write" : "barriers:write");
  const [adding, setAdding] = useState(false);
  const [issued, setIssued] = useState<{ key: IssuedKeyOut; camera: CameraOut } | null>(null);
  const [keysFor, setKeysFor] = useState<CameraOut | null>(null);

  const camerasByLot = useMemo(() => (cameras.data ?? []).filter((c) => !lotId || c.lot_id === lotId), [cameras.data, lotId]);
  const barriersByLot = useMemo(() => (barriers.data ?? []).filter((b) => !lotId || b.lot_id === lotId), [barriers.data, lotId]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.devices")}</h1>
        <div className="w-full sm:w-auto sm:min-w-[260px]">
          <Tabs
            items={[
              { id: "cameras", label: t("nav.cameras"), count: camerasByLot.length },
              { id: "barriers", label: t("nav.barriers"), count: barriersByLot.length },
            ]}
            value={tab}
            onChange={(id) => setParams(id === "cameras" ? {} : { tab: id })}
          />
        </div>
        {canWrite && (
          <Button className="ml-auto" onClick={() => setAdding(true)} data-testid="add-device">
            <Plus className="h-4 w-4" aria-hidden="true" /> {tab === "cameras" ? t("devices.addCamera") : t("devices.addBarrier")}
          </Button>
        )}
      </div>

      {tab === "cameras" ? (
        <Card>
          {cameras.isPending && <SkeletonRows />}
          {cameras.isError && <ErrorBox error={cameras.error} onRetry={() => void cameras.refetch()} />}
          {cameras.data && camerasByLot.length === 0 && <EmptyState title={t("devices.noCameras")} hint={t("devices.noCamerasHint")} testId="cameras-empty" />}
          {cameras.data && camerasByLot.length > 0 && (
            <Table data-testid="cameras-table">
              <thead>
                <tr>
                  <Th>{t("devices.camera")}</Th>
                  <Th>{t("common.code")}</Th>
                  <Th>{t("devices.direction")}</Th>
                  <Th>{t("session.status")}</Th>
                  <Th>{t("devices.lastEvent")}</Th>
                  <Th>{t("devices.key")}</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {camerasByLot.map((c) => (
                  <CameraRow key={c.id} camera={c} timeZone={timeZone} lang={i18n.language} canKeys={hasPermission(me, "cameras:rotate_key")} canWrite={hasPermission(me, "cameras:write")} onKeys={() => setKeysFor(c)} />
                ))}
              </tbody>
            </Table>
          )}
          <p className="mt-3 text-[12.5px] font-semibold text-muted">{t("devices.camerasHint")}</p>
        </Card>
      ) : (
        <Card>
          {barriers.isPending && <SkeletonRows />}
          {barriers.isError && <ErrorBox error={barriers.error} onRetry={() => void barriers.refetch()} />}
          {barriers.data && barriersByLot.length === 0 && <EmptyState title={t("devices.noBarriers")} hint={t("devices.noBarriersHint")} testId="barriers-empty" />}
          {barriers.data && barriersByLot.length > 0 && (
            <Table data-testid="barriers-table">
              <thead>
                <tr>
                  <Th>{t("devices.barrier")}</Th>
                  <Th>{t("devices.driver")}</Th>
                  <Th>{t("devices.endpoint")}</Th>
                  <Th>{t("devices.camera")}</Th>
                  <Th>{t("session.status")}</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {barriersByLot.map((b) => (
                  <BarrierRow key={b.id} barrier={b} cameras={cameras.data ?? []} canOpen={hasPermission(me, "barrier:open")} canWrite={hasPermission(me, "barriers:write")} />
                ))}
              </tbody>
            </Table>
          )}
          <p className="mt-3 text-[12.5px] font-semibold text-muted">{t("devices.barriersHint")}</p>
        </Card>
      )}

      <Drawer open={adding} onClose={() => setAdding(false)} title={tab === "cameras" ? t("devices.newCamera") : t("devices.newBarrier")}>
        {tab === "cameras" ? (
          <CameraForm
            lotId={lotId}
            existing={camerasByLot}
            onDone={(camera, key) => {
              setAdding(false);
              if (key) setIssued({ key, camera });
            }}
          />
        ) : (
          <BarrierForm lotId={lotId} cameras={camerasByLot} onDone={() => setAdding(false)} />
        )}
      </Drawer>

      <Dialog open={issued !== null} onClose={() => setIssued(null)} title={t("devices.keyIssuedTitle")}>
        {issued && <IssuedKeyView issued={issued.key} camera={issued.camera} onClose={() => setIssued(null)} />}
      </Dialog>

      <Drawer open={keysFor !== null} onClose={() => setKeysFor(null)} title={keysFor ? `${t("devices.keys")} · ${keysFor.name}` : ""}>
        {keysFor && <CameraKeys camera={keysFor} timeZone={timeZone} lang={i18n.language} onIssued={(key) => setIssued({ key, camera: keysFor })} />}
      </Drawer>
    </div>
  );
}

function cameraStatus(c: CameraOut, t: (k: string, o?: { defaultValue?: string }) => string): { tone: ReturnType<typeof toneFor>; text: string } {
  if (!c.active) return { tone: "gray", text: t("devices.disabled") };
  if (c.status === "UNKNOWN" && !c.last_heartbeat_at && !c.last_event_at) return { tone: "gray", text: t("devices.neverConnected") };
  return { tone: toneFor(c.status), text: t(`status.${c.status}`, { defaultValue: c.status }) };
}

function CameraRow({ camera: c, timeZone, lang, canKeys, canWrite, onKeys }: { camera: CameraOut; timeZone?: string; lang: string; canKeys: boolean; canWrite: boolean; onKeys: () => void }) {
  const { t } = useTranslation();
  const update = useUpdateCamera();
  const [reasonDialog, askReason] = useReasonDialog(update.isPending);
  const st = cameraStatus(c, t);
  return (
    <tr data-testid="camera-row" data-code={c.code}>
      <Td>
        <div className="font-bold">{c.name}</div>
        <div className="text-[12px] font-semibold text-muted">
          {c.vendor} {c.location ? `· ${c.location}` : ""}
        </div>
      </Td>
      <Td mono>{c.code}</Td>
      <Td>{c.direction === "IN" ? t("live.entry") : t("live.exit")}</Td>
      <Td>
        <StatusDot tone={st.tone}>{st.text}</StatusDot>
      </Td>
      <Td mono>{formatTime(c.last_event_at ?? c.last_heartbeat_at, timeZone, lang)}</Td>
      <Td>
        {canKeys && (
          <Button variant="outline" size="sm" onClick={onKeys} data-testid="camera-keys">
            <KeyRound className="h-4 w-4" aria-hidden="true" /> {t("devices.keys")}
          </Button>
        )}
      </Td>
      <Td>
        {reasonDialog}
        {canWrite && (
          <Button
            variant="ghost"
            size="sm"
            title={c.active ? t("devices.disable") : t("devices.enable")}
            aria-label={c.active ? t("devices.disable") : t("devices.enable")}
            onClick={() =>
              askReason({
                title: `${c.active ? t("devices.disable") : t("devices.enable")} · ${c.name}`,
                confirm: c.active ? t("devices.disable") : t("devices.enable"),
                danger: c.active,
                run: (reason) => update.mutateAsync({ cameraId: c.id, body: { active: !c.active, reason } }).then(() => toast.ok(c.active ? t("devices.disabledToast") : t("devices.enabledToast"))),
              })
            }
          >
            <Power className="h-4 w-4" aria-hidden="true" />
          </Button>
        )}
      </Td>
    </tr>
  );
}

function BarrierRow({ barrier: b, cameras, canOpen, canWrite }: { barrier: BarrierOut; cameras: CameraOut[]; canOpen: boolean; canWrite: boolean }) {
  const { t } = useTranslation();
  const probe = useProbeBarrier();
  const testOpen = useTestOpenBarrier();
  const update = useUpdateBarrier();
  const [reasonDialog, askReason] = useReasonDialog(update.isPending);
  const cam = cameras.find((c) => c.id === b.camera_id);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [editing, setEditing] = useState(false);
  return (
    <tr data-testid="barrier-row" data-name={b.name}>
      <Td>
        <div className="font-bold">{b.name}</div>
        {result && (
          <div className={cn("text-[12px] font-semibold", result.ok ? "text-ok-ink" : "text-bad-ink")} data-testid="barrier-result" data-ok={result.ok ? "1" : "0"}>
            {result.text}
          </div>
        )}
      </Td>
      <Td>{driverLabel(b.driver, t)}</Td>
      <Td mono>{b.endpoint}</Td>
      <Td>{cam ? `${cam.name} · ${cam.code}` : "—"}</Td>
      <Td>
        <StatusDot tone={b.active ? toneFor(b.state) : "gray"}>{b.active ? t(`status.${b.state}`, { defaultValue: b.state }) : t("devices.disabled")}</StatusDot>
      </Td>
      <Td>
        <div className="flex flex-wrap items-center gap-1.5">
          {canWrite && (
            <Button
              variant="outline"
              size="sm"
              disabled={probe.isPending}
              data-testid="barrier-probe"
              title={t("devices.probe")}
              onClick={() =>
                probe.mutate(
                  { barrierId: b.id, reason: t("devices.probeReason") },
                  {
                    onSuccess: (r) =>
                      setResult(
                        r.ok
                          ? { ok: true, text: `${fmt(t("devices.probeOk"), { ms: r.latency_ms ?? 0 })}${r.detail ? ` · ${r.detail}` : ""}` }
                          : { ok: false, text: `${t("devices.probeFail")}: ${r.error ?? ""}` },
                      ),
                    onError: () => setResult({ ok: false, text: t("devices.testFailed") }),
                  },
                )
              }
            >
              <Plug className="h-4 w-4" aria-hidden="true" /> {probe.isPending ? "…" : t("devices.probe")}
            </Button>
          )}
          {canOpen && b.active && (
            <Button
              variant="warn"
              size="sm"
              disabled={testOpen.isPending}
              data-testid="barrier-test"
              onClick={() =>
                testOpen.mutate(
                  { barrierId: b.id, reason: t("devices.testReason") },
                  {
                    onSuccess: (r) => {
                      if (r.state === "OK") {
                        setResult({ ok: true, text: fmt(t("devices.openOk"), { ms: r.ms }) });
                        toast.ok(`${t("devices.testToast")} · ${b.name}`);
                      } else if (r.state === "FAILED") {
                        setResult({ ok: false, text: `${t("devices.openFailed")}: ${r.error ?? ""}` });
                        toast.bad(t("devices.testFailed"));
                      } else {
                        setResult({ ok: false, text: r.state === "SKIPPED" ? t("devices.openSkipped") : t("devices.openPending") });
                      }
                    },
                    onError: () => toast.bad(t("devices.testFailed")),
                  },
                )
              }
            >
              <Zap className="h-4 w-4" aria-hidden="true" /> {testOpen.isPending ? "…" : t("devices.test")}
            </Button>
          )}
          {reasonDialog}
          {canWrite && b.driver === "HIKVISION_ISAPI" && (
            <>
              <Button variant="ghost" size="sm" aria-label={t("devices.editAccess")} title={t("devices.editAccess")} data-testid="barrier-access" onClick={() => setEditing(true)}>
                <Pencil className="h-4 w-4" aria-hidden="true" />
              </Button>
              <Dialog open={editing} onClose={() => setEditing(false)} title={`${t("devices.editAccess")} · ${b.name}`}>
                {editing && (
                  <BarrierAccessForm
                    barrier={b}
                    onDone={() => {
                      setEditing(false);
                      setResult(null);
                    }}
                  />
                )}
              </Dialog>
            </>
          )}
          {canWrite && (
            <Button
              variant="ghost"
              size="sm"
              aria-label={b.active ? t("devices.disable") : t("devices.enable")}
              title={b.active ? t("devices.disable") : t("devices.enable")}
              onClick={() =>
                askReason({
                  title: `${b.active ? t("devices.disable") : t("devices.enable")} · ${b.name}`,
                  confirm: b.active ? t("devices.disable") : t("devices.enable"),
                  danger: b.active,
                  run: (reason) => update.mutateAsync({ barrierId: b.id, body: { active: !b.active, reason } }).then(() => toast.ok(b.active ? t("devices.disabledToast") : t("devices.enabledToast"))),
                })
              }
            >
              <Power className="h-4 w-4" aria-hidden="true" />
            </Button>
          )}
        </div>
      </Td>
    </tr>
  );
}

function suggestCode(direction: "IN" | "OUT", existing: CameraOut[]): string {
  const prefix = direction === "IN" ? "ENTRY_" : "EXIT_";
  const used = new Set(existing.map((c) => c.code));
  for (let i = 1; i < 100; i++) {
    const code = `${prefix}${String(i).padStart(2, "0")}`;
    if (!used.has(code)) return code;
  }
  return `${prefix}${Date.now() % 1000}`;
}

function CameraForm({ lotId, existing, onDone }: { lotId: string | null; existing: CameraOut[]; onDone: (camera: CameraOut, key: IssuedKeyOut | null) => void }) {
  const { t } = useTranslation();
  const create = useCreateCamera();
  const issue = useIssueCameraKey();
  const [name, setName] = useState("");
  const [direction, setDirection] = useState<"IN" | "OUT">("IN");
  const [vendor, setVendor] = useState<"GENERIC" | "HIKVISION" | "DAHUA">("HIKVISION");
  const [code, setCode] = useState("");
  const [location, setLocation] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [confidenceMin, setConfidenceMin] = useState(80);
  const [reviewMin, setReviewMin] = useState(60);
  const [skew, setSkew] = useState(120);
  const effectiveCode = code || suggestCode(direction, existing);
  const busy = create.isPending || issue.isPending;
  const submit = async () => {
    if (!lotId || name.trim().length < 2) return;
    const camera = await create.mutateAsync({
      lot_id: lotId,
      code: effectiveCode,
      name: name.trim(),
      direction,
      vendor,
      location: location.trim() || null,
      confidence_min: confidenceMin,
      confidence_review_min: reviewMin,
      max_clock_skew_seconds: skew,
      heartbeat_interval_sec: 300,
      reason: t("devices.createReason"),
    });
    let key: IssuedKeyOut | null = null;
    try {
      key = await issue.mutateAsync({ cameraId: camera.id, label: t("devices.firstKeyLabel"), reason: t("devices.createReason") });
    } catch {
      toast.warn(t("devices.keyLater"));
    }
    toast.ok(`${t("devices.cameraCreated")} · ${camera.name}`);
    onDone(camera, key);
  };
  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      data-testid="camera-form"
    >
      <Label htmlFor="cam-name">{t("common.name")}</Label>
      <Input id="cam-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("devices.cameraNamePh")} data-testid="camera-name" autoFocus />
      <Label htmlFor="cam-code" className="mt-3">
        {t("devices.codeLabel")}
      </Label>
      <Input id="cam-code" className="font-mono" value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} placeholder={effectiveCode} data-testid="camera-code" />
      <p className="text-[12px] font-semibold text-muted">{t("devices.codeHint")}</p>
      <Eyebrow className="mt-3 mb-1">{t("devices.direction")}</Eyebrow>
      <Segmented value={direction} onChange={setDirection} ariaLabel={t("devices.direction")} options={[{ value: "IN", label: t("live.entry") }, { value: "OUT", label: t("live.exit") }]} />
      <Eyebrow className="mt-3 mb-1">{t("devices.vendor")}</Eyebrow>
      <Segmented
        value={vendor}
        onChange={setVendor}
        ariaLabel={t("devices.vendor")}
        options={[
          { value: "HIKVISION", label: "Hikvision" },
          { value: "DAHUA", label: "Dahua" },
          { value: "GENERIC", label: t("devices.vendorOther") },
        ]}
      />
      <p className="text-[12px] font-semibold text-muted">{t("devices.vendorHint")}</p>
      <Label htmlFor="cam-location" className="mt-3">
        {t("devices.location")}
      </Label>
      <Input id="cam-location" value={location} onChange={(e) => setLocation(e.target.value)} placeholder={t("devices.locationPh")} />
      <button type="button" className="mt-3 self-start text-[12.5px] font-bold text-accent-ink" onClick={() => setAdvanced((v) => !v)} aria-expanded={advanced}>
        {t("devices.advanced")} {advanced ? "▴" : "▾"}
        {!advanced && (
          <span className="ml-2 font-semibold text-muted">
            {confidenceMin} / {reviewMin} · {skew} с
          </span>
        )}
      </button>
      {advanced && (
        <div className="grid grid-cols-3 gap-2">
          <div>
            <Label htmlFor="cam-conf">{t("devices.confidenceMin")}</Label>
            <Input id="cam-conf" type="number" min={0} max={100} value={confidenceMin} onChange={(e) => setConfidenceMin(Number(e.target.value))} />
          </div>
          <div>
            <Label htmlFor="cam-review">{t("devices.reviewMin")}</Label>
            <Input id="cam-review" type="number" min={0} max={100} value={reviewMin} onChange={(e) => setReviewMin(Number(e.target.value))} />
          </div>
          <div>
            <Label htmlFor="cam-skew">{t("devices.skew")}</Label>
            <Input id="cam-skew" type="number" min={0} max={3600} value={skew} onChange={(e) => setSkew(Number(e.target.value))} />
          </div>
        </div>
      )}
      {create.isError && <ErrorBox error={create.error} />}
      <div className="mt-4 rounded-xl bg-surface-2 p-3 text-[12.5px] font-semibold text-ink-2">{t("devices.whatNext")}</div>
      <div className="mt-4 flex gap-2">
        <Button type="submit" className="ml-auto" disabled={busy || !lotId || name.trim().length < 2} data-testid="camera-submit">
          {t("devices.createAndKey")}
        </Button>
      </div>
    </form>
  );
}

function IssuedKeyView({ issued, camera, onClose }: { issued: IssuedKeyOut; camera: CameraOut; onClose: () => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const origin = window.location.origin;
  // Hikvision/Dahua в режиме «HTTP-хост» не умеют свой заголовок — ключ идёт последним сегментом адреса
  const keyInPath = camera.vendor !== "GENERIC";
  const path = keyInPath ? `/api/v1/camera/vendor/${camera.vendor.toLowerCase()}/${issued.key}` : "/api/v1/camera/events";
  const server = new URL(origin);
  const port = server.port || (server.protocol === "https:" ? "443" : "80");
  // панель открыта как localhost — камера по такому адресу сервер не найдёт: нужен адрес компьютера в сети
  const localOnly = /^(localhost|127\.|\[::1\])/.test(server.hostname);
  const copy = async (text: string = issued.key) => {
    try {
      await copyToClipboard(text);
      setCopied(true);
      toast.ok(t("devices.copied"));
    } catch {
      toast.warn(t("devices.copyFailed"));
    }
  };
  const memo = () => {
    const text = [
      `${t("devices.memoTitle")}: ${camera.name} (${camera.code})`,
      ...(keyInPath
        ? [
            `${t("devices.memoHost")}: ${server.hostname}`,
            `${t("devices.memoPort")}: ${port}${server.protocol === "https:" ? " (HTTPS)" : ""}`,
            `URL: ${path}`,
            t("devices.memoNoHeader"),
          ]
        : [`${t("devices.memoServer")}: ${origin}${path}`, `X-Camera-Key: ${issued.key}`]),
      `${t("devices.memoWhere")}: ${t(`devices.memo.${camera.vendor}`)}`,
    ].join("\n");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `camera-${camera.code}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <div className="space-y-3" data-testid="issued-key">
      <p className="text-[14px] font-semibold text-ink-2">{t(keyInPath ? "devices.keyIssuedHintPath" : "devices.keyIssuedHint")}</p>
      <div className="flex items-center gap-3 rounded-xl bg-ink px-4 py-3 font-mono text-[14px] font-bold text-[#c9f5da] break-all">
        <span>
          <span className="text-white">{issued.key_prefix}</span>
          {issued.key.slice(issued.key_prefix.length)}
        </span>
        <Button variant="secondary" size="sm" className="ml-auto flex-none" onClick={() => void copy(issued.key)} data-testid="copy-key">
          <Copy className="h-4 w-4" aria-hidden="true" /> {copied ? t("devices.copied") : t("error.copy")}
        </Button>
      </div>
      {keyInPath ? (
        <div className="space-y-2 text-[13px] font-semibold text-ink-2" data-testid="camera-push-target">
          {localOnly && <p className="rounded-lg bg-warn-soft px-3 py-2 text-warn-ink">{t("devices.localhostWarn")}</p>}
          <dl className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 rounded-xl bg-surface-2 p-3">
            <dt className="text-muted">{t("devices.memoHost")}</dt>
            <dd className="font-mono text-ink">{server.hostname}</dd>
            <span />
            <dt className="text-muted">{t("devices.memoPort")}</dt>
            <dd className="font-mono text-ink">{port}</dd>
            <span />
            <dt className="text-muted">{t("devices.memoUrl")}</dt>
            <dd className="font-mono text-[11.5px] break-all text-ink" data-testid="camera-push-url">
              {path}
            </dd>
            <Button variant="ghost" size="sm" onClick={() => void copy(path)} aria-label={t("error.copy")}>
              <Copy className="h-4 w-4" aria-hidden="true" />
            </Button>
          </dl>
          <p>{t(`devices.memo.${camera.vendor}`)}</p>
        </div>
      ) : (
        <ol className="grid gap-2 text-[13px] font-semibold text-ink-2 sm:grid-cols-3">
          <li className="rounded-xl bg-surface-2 p-3">
            <b className="block text-ink">1 · {t("devices.memoServer")}</b>
            <span className="font-mono text-[11.5px] break-all">
              {origin}
              {path}
            </span>
          </li>
          <li className="rounded-xl bg-surface-2 p-3">
            <b className="block text-ink">2 · {t("devices.memoHeader")}</b>
            <span className="font-mono text-[11.5px]">X-Camera-Key</span>
          </li>
          <li className="rounded-xl bg-surface-2 p-3">
            <b className="block text-ink">3 · {t("devices.memoWhere")}</b>
            {t(`devices.memo.${camera.vendor}`)}
          </li>
        </ol>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={memo} data-testid="download-memo">
          {t("devices.downloadMemo")}
        </Button>
        <Button className="ml-auto" onClick={onClose} data-testid="key-done">
          {t("devices.keySaved")}
        </Button>
      </div>
    </div>
  );
}

function CameraKeys({ camera, timeZone, lang, onIssued }: { camera: CameraOut; timeZone?: string; lang: string; onIssued: (k: IssuedKeyOut) => void }) {
  const { t } = useTranslation();
  const keys = useCameraKeys(camera.id);
  const issue = useIssueCameraKey();
  const revoke = useRevokeCameraKey();
  const [reasonDialog, askReason] = useReasonDialog(issue.isPending || revoke.isPending);
  return (
    <div className="space-y-3">
      {reasonDialog}
      {keys.isPending && <SkeletonRows rows={2} />}
      {keys.isError && <ErrorBox error={keys.error} />}
      {keys.data && (
        <ul className="space-y-2" data-testid="camera-keys-list">
          {keys.data.map((k) => (
            <li key={k.id} className="flex items-center gap-3 rounded-xl border border-line p-3">
              <div className="min-w-0">
                <div className="font-mono text-[13px] font-bold">{k.key_prefix}…</div>
                <div className="text-[12px] font-semibold text-muted">
                  {k.label ?? "—"} · {k.active ? t("common.active") : t("devices.revoked")} · {t("devices.lastUsed")} {formatTime(k.last_used_at, timeZone, lang)}
                </div>
              </div>
              {k.active && (
                <Button
                  variant="outline"
                  size="sm"
                  className="ml-auto"
                  onClick={() =>
                    askReason({
                      title: `${t("devices.revoke")} · ${k.key_prefix}…`,
                      confirm: t("devices.revoke"),
                      run: (reason) => revoke.mutateAsync({ keyId: k.id, reason }).then(() => toast.ok(t("devices.revokedToast"))),
                    })
                  }
                >
                  {t("devices.revoke")}
                </Button>
              )}
            </li>
          ))}
          {keys.data.length === 0 && <EmptyState title={t("devices.noKeys")} />}
        </ul>
      )}
      <Button
        disabled={issue.isPending}
        onClick={() =>
          askReason({
            title: `${t("devices.issueKey")} · ${camera.name}`,
            confirm: t("devices.issueKey"),
            danger: false,
            defaultReason: t("devices.reissueReason"),
            run: (reason) => issue.mutateAsync({ cameraId: camera.id, label: null, reason }).then((k) => onIssued(k)),
          })
        }
        data-testid="issue-key"
      >
        <KeyRound className="h-4 w-4" aria-hidden="true" /> {t("devices.issueKey")}
      </Button>
      <p className="text-[12px] font-semibold text-muted">{t("devices.issueHint")}</p>
    </div>
  );
}

function BarrierForm({ lotId, cameras, onDone }: { lotId: string | null; cameras: CameraOut[]; onDone: () => void }) {
  const { t } = useTranslation();
  const create = useCreateBarrier();
  const [name, setName] = useState("");
  const [driver, setDriver] = useState<Driver>("HIKVISION_ISAPI");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [cameraId, setCameraId] = useState<string>(cameras[0]?.id ?? "");
  const [secretRef, setSecretRef] = useState("");
  const [autoClose, setAutoClose] = useState("");
  const hint = endpointHint(endpoint, driver, t);
  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        if (!lotId) return;
        create.mutate(
          {
            lot_id: lotId,
            name: name.trim(),
            driver,
            endpoint: endpoint.trim(),
            camera_id: cameraId || null,
            auth_secret_ref: driver === "HIKVISION_ISAPI" ? null : secretRef.trim() || null,
            auto_close_seconds: autoClose ? Number(autoClose) : null,
            ...(driver === "HIKVISION_ISAPI" ? { username: username.trim(), password } : {}),
            reason: t("devices.createReason"),
          },
          {
            onSuccess: (b) => {
              toast.ok(`${t("devices.barrierCreated")} · ${b.name}`);
              onDone();
            },
          },
        );
      }}
      data-testid="barrier-form"
    >
      <Label htmlFor="bar-name">{t("common.name")}</Label>
      <Input id="bar-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("devices.barrierNamePh")} data-testid="barrier-name" autoFocus />
      <Label htmlFor="bar-camera" className="mt-3">
        {t("devices.opensBy")}
      </Label>
      <Select id="bar-camera" value={cameraId} onChange={(e) => setCameraId(e.target.value)}>
        <option value="">—</option>
        {cameras.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name} · {c.code}
          </option>
        ))}
      </Select>
      <Eyebrow className="mt-3 mb-1">{t("devices.driver")}</Eyebrow>
      <Segmented
        value={driver}
        onChange={setDriver}
        ariaLabel={t("devices.driver")}
        options={(["HIKVISION_ISAPI", "MODBUS_TCP", "HTTP_REST"] as const).map((d) => ({ value: d, label: driverLabel(d, t) }))}
      />
      {driver === "HIKVISION_ISAPI" && <p className="mt-1 rounded-lg bg-surface-2 px-3 py-2 text-[12.5px] font-semibold text-ink-2">{t("devices.hikvisionHint")}</p>}
      <Label htmlFor="bar-endpoint" className="mt-3">
        {t("devices.endpoint")}
      </Label>
      <Input id="bar-endpoint" className="font-mono" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder={ENDPOINT_PLACEHOLDER[driver]} data-testid="barrier-endpoint" />
      {hint && <div className={`mt-1 rounded-lg px-3 py-2 text-[12.5px] font-semibold ${hint.ok ? "bg-ok-soft text-ok-ink" : "bg-warn-soft text-warn-ink"}`}>{hint.text}</div>}
      {driver === "HIKVISION_ISAPI" ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <div>
            <Label htmlFor="bar-user">{t("devices.cameraLogin")}</Label>
            <Input id="bar-user" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" data-testid="barrier-username" />
          </div>
          <div>
            <Label htmlFor="bar-pass">{t("devices.cameraPassword")}</Label>
            <Input id="bar-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" data-testid="barrier-password" />
          </div>
          <p className="text-[12px] font-semibold text-muted sm:col-span-2">{t("devices.cameraPasswordHint")}</p>
        </div>
      ) : (
        <>
          <Label htmlFor="bar-secret" className="mt-3">
            {t("devices.secretRef")} {driver === "HTTP_REST" && <span className="text-bad-ink">*</span>}
          </Label>
          <Input id="bar-secret" className="font-mono" value={secretRef} onChange={(e) => setSecretRef(e.target.value)} placeholder="kv/tenants/<tenant>/barriers/<name>" data-testid="barrier-secret" />
          <p className="text-[12px] font-semibold text-muted">{t("devices.secretHint")}</p>
        </>
      )}
      <Label htmlFor="bar-autoclose" className="mt-3">
        {t("devices.autoClose")}
      </Label>
      <Input id="bar-autoclose" type="number" min={1} max={600} value={autoClose} onChange={(e) => setAutoClose(e.target.value)} placeholder="10" />
      <p className="text-[12px] font-semibold text-muted">{t("devices.autoCloseHint")}</p>
      {create.isError && <ErrorBox error={create.error} />}
      <div className="mt-4 flex gap-2">
        <Button type="submit" className="ml-auto" disabled={
            create.isPending ||
            !lotId ||
            name.trim().length < 2 ||
            endpoint.trim().length < 3 ||
            (driver === "HTTP_REST" && secretRef.trim().length < 3) ||
            (driver === "HIKVISION_ISAPI" && (!username.trim() || !password))
          } data-testid="barrier-submit">
          {t("devices.addBarrier")}
        </Button>
      </div>
    </form>
  );
}

/* Адрес камеры и её логин/пароль у стрелы Hikvision: пароль не возвращается сервером, поэтому меняется только вводом
   заново; сервер требует пароль и при смене адреса (иначе сохранённый пароль ушёл бы на чужое устройство). */
function BarrierAccessForm({ barrier, onDone }: { barrier: BarrierOut; onDone: () => void }) {
  const { t } = useTranslation();
  const update = useUpdateBarrier();
  const [endpoint, setEndpoint] = useState(barrier.endpoint);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const hint = endpointHint(endpoint, "HIKVISION_ISAPI", t);
  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        const changed = endpoint.trim() !== barrier.endpoint;
        update.mutate(
          {
            barrierId: barrier.id,
            body: { ...(changed ? { endpoint: endpoint.trim() } : {}), username: username.trim(), password, reason: t("devices.accessReason") },
          },
          {
            onSuccess: () => {
              toast.ok(t("devices.accessSaved"));
              onDone();
            },
          },
        );
      }}
      data-testid="barrier-access-form"
    >
      <p className="rounded-lg bg-surface-2 px-3 py-2 text-[12.5px] font-semibold text-ink-2">{t("devices.accessHint")}</p>
      <Label htmlFor="acc-endpoint" className="mt-3">
        {t("devices.endpoint")}
      </Label>
      <Input id="acc-endpoint" className="font-mono" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder={ENDPOINT_PLACEHOLDER.HIKVISION_ISAPI} data-testid="access-endpoint" />
      {hint && <div className={`mt-1 rounded-lg px-3 py-2 text-[12.5px] font-semibold ${hint.ok ? "bg-ok-soft text-ok-ink" : "bg-warn-soft text-warn-ink"}`}>{hint.text}</div>}
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="acc-user">{t("devices.cameraLogin")}</Label>
          <Input id="acc-user" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" data-testid="access-username" />
        </div>
        <div>
          <Label htmlFor="acc-pass">{t("devices.cameraPassword")}</Label>
          <Input id="acc-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" data-testid="access-password" autoFocus />
        </div>
      </div>
      {update.isError && <ErrorBox error={update.error} />}
      <div className="mt-4 flex gap-2">
        <Button type="button" variant="ghost" onClick={onDone}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" className="ml-auto" disabled={update.isPending || endpoint.trim().length < 3 || !username.trim() || !password} data-testid="access-submit">
          {update.isPending ? "…" : t("devices.accessSave")}
        </Button>
      </div>
    </form>
  );
}

type Driver = "HIKVISION_ISAPI" | "MODBUS_TCP" | "HTTP_REST";
const DRIVER_LABEL: Record<string, string> = { MODBUS_TCP: "Modbus TCP", HTTP_REST: "HTTP REST" }; // технические названия
const driverLabel = (d: string, t: (key: string) => string) => (d === "HIKVISION_ISAPI" ? t("devices.driverHikvision") : (DRIVER_LABEL[d] ?? d));
const ENDPOINT_PLACEHOLDER: Record<Driver, string> = {
  HIKVISION_ISAPI: "http://192.168.1.64",
  MODBUS_TCP: "10.100.5.21:502?unit=1&open_coil=0&close_coil=1&pulse_ms=500",
  HTTP_REST: "http://10.100.5.20/relay",
};

/* Подсказка по адресу до сохранения: приватные адреса проходят только внутри allowlist площадки (проверяет сервер);
   здесь — грубая проверка формы адреса, чтобы монтажник увидел ошибку сразу. */
function endpointHint(endpoint: string, driver: Driver, t: (k: string) => string): { ok: boolean; text: string } | null {
  const v = endpoint.trim();
  if (!v) return null;
  if (driver === "HTTP_REST" && !/^https?:\/\//.test(v)) return { ok: false, text: t("devices.hintHttp") };
  const host = v.replace(/^https?:\/\//, "").split(/[/?:]/)[0] ?? "";
  if (/^(127\.|169\.254\.|0\.|localhost$)/.test(host)) return { ok: false, text: t("devices.hintBlocked") };
  if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host)) return { ok: true, text: t("devices.hintPrivate") };
  return { ok: true, text: t("devices.hintPublic") };
}
