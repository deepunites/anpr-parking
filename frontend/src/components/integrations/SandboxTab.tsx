import { useEffect, useMemo, useState } from "react";
import { FlaskConical } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useCameras, useSandbox, type CameraOut, type SandboxOut } from "@/api/hooks";
import { useLotStore } from "@/lib/lot";
import { Card, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label, Segmented, Select, Textarea } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty";
import { ErrorBox } from "@/components/ErrorBox";

type Vendor = "generic" | "hikvision" | "dahua";

// eslint-disable-next-line react-refresh/only-export-components -- образцы нужны и тестам
export function samplePayload(vendor: Vendor, camera: CameraOut | undefined, plate = "01A777AA"): string {
  const now = new Date().toISOString();
  const entry = (camera?.direction ?? "IN") === "IN";
  if (vendor === "hikvision") {
    return [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">',
      `  <ipAddress>10.10.1.5</ipAddress><dateTime>${now}</dateTime>`,
      "  <eventType>ANPR</eventType>",
      `  <ANPR><licensePlate>${plate}</licensePlate><confidenceLevel>96</confidenceLevel>`,
      `    <direction>${entry ? "forward" : "reverse"}</direction><country>UZ</country></ANPR>`,
      "</EventNotificationAlert>",
    ].join("\n");
  }
  if (vendor === "dahua") {
    return JSON.stringify(
      { Picture: { Plate: { PlateNumber: plate, Confidence: 94, PlateType: "Normal" }, SnapInfo: { Direction: entry ? "Enter" : "Leave", SnapTime: now } } },
      null,
      2,
    );
  }
  return JSON.stringify(
    {
      camera_code: camera?.code ?? "ENTRY_01",
      event_type: entry ? "ENTRY" : "EXIT",
      plate_number: plate,
      confidence: 95,
      event_time: now,
      direction: camera?.direction ?? "IN",
      recognition: { provider: "sandbox", version: "1.0" },
    },
    null,
    2,
  );
}

/* Песочница: событие камеры проходит те же проверки, что и настоящее, но сессия не создаётся и стрела не
   открывается. Итог — принято/отклонено с причиной и решение доступа по номеру. */
export function SandboxTab() {
  const { t } = useTranslation();
  const cameras = useCameras();
  const { lotId } = useLotStore();
  const list = useMemo(() => (cameras.data ?? []).filter((c) => !lotId || c.lot_id === lotId), [cameras.data, lotId]);
  const [cameraId, setCameraId] = useState<string>("");
  const camera = list.find((c) => c.id === cameraId) ?? list[0];
  const [vendor, setVendor] = useState<Vendor>("hikvision");
  const [payload, setPayload] = useState("");
  const run = useSandbox();

  useEffect(() => {
    setPayload(samplePayload(vendor, camera));
  }, [vendor, camera]);

  if (!cameras.isLoading && list.length === 0) {
    return <EmptyState title={t("integrations.sandbox.noCameras")} hint={t("integrations.sandbox.noCamerasHint")} />;
  }
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
      <Card>
        <CardTitle>
          <FlaskConical className="mr-2 inline h-5 w-5" aria-hidden="true" /> {t("integrations.sandbox.title")}
        </CardTitle>
        <p className="mb-3 text-[13px] font-semibold text-ink-2">{t("integrations.sandbox.hint")}</p>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (camera) run.mutate({ camera_id: camera.id, vendor, payload });
          }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="sb-camera">{t("devices.camera")}</Label>
              <Select id="sb-camera" value={camera?.id ?? ""} onChange={(e) => setCameraId(e.target.value)} data-testid="sandbox-camera">
                {list.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.code}
                  </option>
                ))}
              </Select>
            </div>
            <div>
              <span className="mb-1 block text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("devices.vendor")}</span>
              <Segmented
                ariaLabel={t("devices.vendor")}
                value={vendor}
                onChange={setVendor}
                options={[
                  { value: "hikvision", label: "Hikvision" },
                  { value: "dahua", label: "Dahua" },
                  { value: "generic", label: "JSON" },
                ]}
              />
            </div>
          </div>
          <div>
            <Label htmlFor="sb-payload">{t("integrations.sandbox.payload")}</Label>
            <Textarea id="sb-payload" value={payload} onChange={(e) => setPayload(e.target.value)} className="min-h-64 font-mono text-[12.5px]" spellCheck={false} data-testid="sandbox-payload" />
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={() => setPayload(samplePayload(vendor, camera))}>{t("integrations.sandbox.reset")}</Button>
            <Button type="submit" className="ml-auto" disabled={run.isPending || !camera} data-testid="sandbox-run">
              {t("integrations.sandbox.run")}
            </Button>
          </div>
        </form>
      </Card>
      <Card className="self-start" data-testid="sandbox-result">
        <CardTitle>{t("integrations.sandbox.result")}</CardTitle>
        {run.error ? <ErrorBox error={run.error} /> : null}
        {!run.data && !run.error && <p className="text-[13px] font-semibold text-muted">{t("integrations.sandbox.idle")}</p>}
        {run.data && <SandboxResult r={run.data} />}
      </Card>
    </div>
  );
}

function SandboxResult({ r }: { r: SandboxOut }) {
  const { t } = useTranslation();
  const rows: [string, React.ReactNode][] = [
    [t("integrations.sandbox.plate"), r.plate ? <span className="font-mono">{r.plate}</span> : "—"],
    [t("integrations.sandbox.event"), r.event_type ?? "—"],
    [t("integrations.sandbox.confidence"), r.confidence !== null && r.confidence !== undefined ? `${r.confidence}%${r.confidence_class && r.confidence_class !== "NORMAL" ? ` · ${r.confidence_class}` : ""}` : "—"],
  ];
  if (r.accepted) {
    rows.push([t("integrations.sandbox.access"), r.access_kind ?? "—"]);
    rows.push([t("integrations.sandbox.barrier"), r.opens_barrier ? t("integrations.sandbox.opens") : t("integrations.sandbox.staysClosed")]);
    if (r.late) rows.push([t("integrations.sandbox.late"), t("integrations.sandbox.lateYes")]);
  }
  return (
    <div className="space-y-3" data-accepted={r.accepted ? "true" : "false"}>
      {r.accepted ? (
        <Badge tone="green">{t("integrations.sandbox.accepted")}</Badge>
      ) : (
        <div className="rounded-xl bg-bad-soft p-3 text-bad-ink">
          <div className="font-mono text-[13px] font-extrabold" data-testid="sandbox-reason">{r.reason}</div>
          <div className="text-[13px] font-semibold">{t(`integrations.reasonText.${r.reason}`, { defaultValue: r.message ?? "" })}</div>
        </div>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13.5px]">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="font-semibold text-muted">{k}</dt>
            <dd className="font-bold">{v}</dd>
          </div>
        ))}
      </dl>
      <p className="text-[12px] font-semibold text-muted">{t("integrations.sandbox.noSideEffects")}</p>
    </div>
  );
}
