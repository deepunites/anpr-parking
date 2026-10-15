import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Car, Pencil, Printer, Video, X } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { ApiError } from "@/api/errors";
import { keys, useConfirmCash, useCorrectPlate, useOpenExit, usePrepayCash, useQuote, useSession, useSessionImages, useSessionPayments, useSessionVideos, useSetSessionVehicle, usePrintReceipt } from "@/api/hooks";
import { PlateInput, isPlateValid } from "@/components/vehicle/PlateInput";
import { VehicleFields } from "@/components/vehicle/VehicleFields";
import { colorSwatch, vehicleLabel, type VehicleValue } from "@/components/vehicle/vehicle";
import { cn } from "@/lib/cn";
import { hasPermission, useAuthStore } from "@/auth/store";
import { Button } from "@/components/ui/button";
import { Label, Textarea } from "@/components/ui/input";
import { Card, Eyebrow } from "@/components/ui/card";
import { Badge, toneFor } from "@/components/ui/badge";
import { Plate } from "@/components/ui/plate";
import { Money } from "@/components/ui/money";
import { Dialog } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { formatDuration, formatMinor, formatTime } from "@/lib/format";

function ReasonForm({ onSubmit, submitLabel, busy, children, testId }: { onSubmit: (reason: string) => void; submitLabel: string; busy: boolean; children?: React.ReactNode; testId: string }) {
  const { t } = useTranslation();
  const [reason, setReason] = useState("");
  const valid = reason.trim().length >= 3;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (valid) onSubmit(reason.trim());
  };
  return (
    <form onSubmit={submit} className="space-y-3">
      {children}
      <div>
        <Label htmlFor={`${testId}-reason`}>{t("session.reason")}</Label>
        <Textarea id={`${testId}-reason`} required minLength={3} value={reason} onChange={(e) => setReason(e.target.value)} data-testid={`${testId}-reason`} />
        {!valid && <p className="mt-1 text-[12.5px] font-semibold text-muted">{t("session.reasonRequired")}</p>}
      </div>
      <Button type="submit" size="lg" disabled={!valid || busy} data-testid={`${testId}-submit`}>
        {submitLabel}
      </Button>
    </form>
  );
}

function Photos({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const images = useSessionImages(sessionId);
  if (images.isPending) return <Skeleton className="aspect-[4/3] w-full" />;
  if (images.isError || !images.data?.length)
    return (
      <div className="grid aspect-[4/3] w-full place-items-center rounded-xl bg-[linear-gradient(160deg,var(--surface-3),var(--surface-2))] text-[13px] font-semibold text-muted" data-testid="no-photos">
        {t("session.noPhotos")}
      </div>
    );
  return (
    <div className="grid gap-2">
      {images.data.slice(0, 2).map((img) => (
        <img key={img.event_id} src={img.url} alt={`${t("session.photos")} ${formatTime(img.event_time, undefined, "ru")}`} className="aspect-[4/3] w-full rounded-xl border border-line object-cover" loading="lazy" />
      ))}
    </div>
  );
}

/** Карточка сессии: табличка номера, кадр, факты, сумма с сервера и три действия в фиксированном порядке —
    принять наличные · открыть выезд · исправить номер (I-15/I-16: причина обязательна, исходное распознавание сохраняется). */
/* Видео въезда и выезда из записи камер на сервере площадки: ролик грузится только по нажатию (в карточке их
   десятки за смену), ссылка подписана и живёт минуты — перед показом список перезапрашивается. Записи на сервере
   нет вовсе — блок не показывается. */
function SessionVideos({ sessionId, timeZone }: { sessionId: string; timeZone?: string }) {
  const { t, i18n } = useTranslation();
  const videos = useSessionVideos(sessionId);
  const [playing, setPlaying] = useState<{ kind: string; url: string } | null>(null);
  if (!videos.data?.enabled || videos.data.videos.length === 0) return null;
  const label = (kind: string) => (kind === "ENTRY" ? t("session.videoEntry") : t("session.videoExit"));
  const play = async (kind: string) => {
    const fresh = await videos.refetch();
    const video = fresh.data?.videos.find((v) => v.kind === kind);
    if (video?.url) setPlaying({ kind, url: video.url });
  };
  return (
    <div data-testid="session-videos">
      <Eyebrow className="mb-1">{t("session.video")}</Eyebrow>
      <div className="flex flex-wrap gap-2">
        {videos.data.videos.map((v) => (
          <Button key={v.kind} variant="outline" size="sm" disabled={!v.available} onClick={() => void play(v.kind)} data-testid="session-video" data-kind={v.kind}>
            <Video className="h-4 w-4" aria-hidden="true" />
            {label(v.kind)} {formatTime(v.event_time, timeZone, i18n.language)}
            {!v.available && <span className="font-semibold text-muted">· {t("session.noVideo")}</span>}
          </Button>
        ))}
      </div>
      <Dialog open={playing !== null} onClose={() => setPlaying(null)} title={`${t("session.videoTitle")} · ${playing ? label(playing.kind) : ""}`}>
        {playing && <video key={playing.url} src={playing.url} controls autoPlay muted playsInline className="aspect-video w-full rounded-xl bg-black" data-testid="session-video-player" />}
      </Dialog>
    </div>
  );
}

export function SessionPanel({ sessionId, timeZone, onClose }: { sessionId: string; timeZone?: string; onClose?: () => void }) {
  const { t, i18n } = useTranslation();
  const me = useAuthStore((s) => s.me);
  const session = useSession(sessionId);
  const payments = useSessionPayments(sessionId);
  const saga = useQuery({
    queryKey: keys.saga(sessionId),
    queryFn: async () => {
      const result = await api.GET("/api/v1/sessions/{session_id}/saga", { params: { path: { session_id: sessionId } } });
      if (result.response.status === 404) return null;
      return unwrap(result);
    },
    retry: false,
  });
  const correct = useCorrectPlate(sessionId);
  const openExit = useOpenExit(sessionId);
  const confirmCash = useConfirmCash(sessionId);
  const prepay = usePrepayCash(sessionId);
  const printReceipt = usePrintReceipt();
  // оплата принята → чек сразу на принтер кассы; принтер не настроен — молча, выключен — предупреждение (оплата уже есть)
  const { mutate: sendReceipt } = printReceipt;
  const print = useCallback(
    (paymentId: string, explicit = false) =>
      sendReceipt(paymentId, {
        onSuccess: () => toast.ok(t("session.receiptPrinted")),
        onError: (e) => {
          if (explicit || (e instanceof ApiError ? e.code : "") !== "PRINTER_NOT_CONFIGURED") toast.warn(t("session.receiptFailed"));
        },
      }),
    [sendReceipt, t],
  );
  const onPaid = useCallback(
    (payment: { id: string }) => {
      toast.ok(t("session.confirmed"));
      print(payment.id);
    },
    [print, t],
  );
  const [correcting, setCorrecting] = useState(false);
  const [opening, setOpening] = useState(false);
  const [newPlate, setNewPlate] = useState("");
  const [given, setGiven] = useState<number | null>(null); // внесено наличными — только для подсказки сдачи, на сервер не уходит
  const plateInput = useRef<HTMLInputElement>(null);
  const canWrite = hasPermission(me, "sessions:write");
  const canOpen = hasPermission(me, "barrier:open");
  const canCash = hasPermission(me, "payment:create_cash");
  const pending = (payments.data ?? []).find((p) => p.status === "PENDING");
  const paid = (payments.data ?? []).some((p) => p.status === "PAID");
  const isActive = session.data?.status === "ACTIVE" || session.data?.status === "MANUAL_REVIEW";
  const quote = useQuote(sessionId, canCash && isActive && !pending && !paid);

  useEffect(() => {
    if (correcting) plateInput.current?.focus(); // поле ввода уже в фокусе (§3)
  }, [correcting]);

  // Ctrl+Enter — принять наличные (обязательство) или предоплату, не тянуться к мыши
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.key !== "Enter" || correcting || opening) return;
      if (canCash && pending && !confirmCash.isPending) {
        e.preventDefault();
        confirmCash.mutate({ paymentId: pending.id, amountMinor: pending.amount_minor }, { onSuccess: onPaid });
      } else if (canCash && !pending && quote.data && isActive && !prepay.isPending) {
        e.preventDefault();
        prepay.mutate({ amount_minor: quote.data.amount_minor }, { onSuccess: onPaid });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [canCash, pending, quote.data, isActive, confirmCash, prepay, correcting, opening, t, onPaid]);

  if (session.isPending) return <Skeleton className="h-64 w-full" />;
  if (session.isError) return <ErrorBox error={session.error} onRetry={() => void session.refetch()} />;
  const s = session.data;
  const review = s.status === "MANUAL_REVIEW";

  return (
    <Card data-testid="session-panel" className="flex h-full flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <span data-testid="session-plate">
          <Plate plate={s.plate} size="xl" />
        </span>
        <div className="min-w-0">
          <VehicleLine sessionId={s.id} make={s.vehicle_make} model={s.vehicle_model} color={s.vehicle_color} canEdit={canWrite} />
          <div className="text-[12.5px] font-semibold text-muted">
            {t("session.entry")} {formatTime(s.entry_time, timeZone, i18n.language)}
            {s.entry_manual && <span className="ml-1.5 font-bold text-warn-ink">· {t("manual.badge")}</span>}
          </div>
          <div className="font-mono text-[12px] font-semibold text-muted">{s.id.slice(0, 8)}</div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Badge tone={toneFor(s.status)}>{t(`status.${s.status}`, { defaultValue: s.status })}</Badge>
          {onClose && (
            <Button variant="ghost" size="icon" onClick={onClose} aria-label={t("common.close")}>
              <X className="h-5 w-5" aria-hidden="true" />
            </Button>
          )}
        </div>
      </div>
      {review && s.review_reason && (
        <div className="rounded-xl bg-warn-soft px-3 py-2 text-[13px] font-bold text-warn-ink" data-testid="review-reason">
          {t("live.review")}: {s.review_reason}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Photos sessionId={s.id} />
        <div className="grid content-start grid-cols-2 gap-2">
          <Fact label={t("session.minutes")} value={formatDuration(s.billing_minutes)} />
          <Fact label={t("session.exitTime")} value={formatTime(s.exit_time, timeZone, i18n.language)} />
          <Fact label={t("session.sagaState")} value={saga.data ? t(`saga.${saga.data.state}`, { defaultValue: saga.data.state }) : "—"} />
          <Fact label={t("session.access")} value={s.entry_access_kind ? t(`access.${s.entry_access_kind}`, { defaultValue: s.entry_access_kind }) : "—"} />
        </div>
      </div>

      <SessionVideos sessionId={s.id} timeZone={timeZone} />

      {/* Платежи: сумма только с бэкенда, оператор её не редактирует */}
      <div>
        <Eyebrow className="mb-1">{t("session.payments")}</Eyebrow>
        {payments.isError && <ErrorBox error={payments.error} />}
        {(payments.data ?? []).length === 0 && <p className="text-[12.5px] font-semibold text-muted">{t("session.noPayments")}</p>}
        <ul className="space-y-1">
          {(payments.data ?? []).map((p) => (
            <li key={p.id} className="flex items-center gap-2 text-[13.5px]" data-testid="payment-row" data-status={p.status}>
              <Badge tone={toneFor(p.status)}>{t(`status.${p.status}`, { defaultValue: p.status })}</Badge>
              <Money formatted={p.amount.formatted} size="sm" />
              <span className="font-semibold text-muted">{p.method ?? ""}</span>
              {p.status === "PAID" && (
                <Button variant="ghost" size="sm" className="ml-auto" disabled={printReceipt.isPending} onClick={() => print(p.id, true)} data-testid="print-receipt">
                  <Printer className="h-4 w-4" aria-hidden="true" /> {t("session.printReceipt")}
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>

      {canCash && pending && (
        <div className="flex flex-wrap items-center gap-4 rounded-[14px] border border-ok/40 bg-ok-soft px-4 py-3">
          <div>
            <Money formatted={pending.amount.formatted} size="xl" tone="ok" className="block" />
            <div className="mt-1 text-[12.5px] font-semibold text-ink-2">
              <span data-testid="amount-due">{pending.amount.formatted}</span> · {t("session.amountDue").toLowerCase()} · {t("session.amountFromServer")}
            </div>
          </div>
          <div className="ml-auto w-full sm:w-auto">
            <div className="text-[10.5px] font-bold uppercase tracking-[0.08em] text-muted">{t("cash.given")}</div>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {[pending.amount_minor, 1_000_000, 2_000_000, 5_000_000, 10_000_000].filter((v, i, a) => a.indexOf(v) === i).map((v) => (
                <button key={v} type="button" onClick={() => setGiven(v)} className={`rounded-lg px-2.5 py-1.5 font-mono text-[12.5px] font-bold ${given === v ? "bg-ink text-ground" : "bg-surface text-ink-2"}`} data-testid="cash-quick">
                  {formatMinor(v, i18n.language)}
                </button>
              ))}
            </div>
            {given !== null && given >= pending.amount_minor && (
              <div className="mt-1.5 text-[13px] font-bold text-ink" data-testid="cash-change">
                {t("cash.change")}: <span className="font-mono">{formatMinor(given - pending.amount_minor, i18n.language)}</span>
              </div>
            )}
          </div>
          <ErrorBox error={confirmCash.error} />
        </div>
      )}
      {canCash && !pending && quote.data && isActive && (
        <div className="flex flex-wrap items-center gap-4 rounded-[14px] border border-ok/40 bg-ok-soft px-4 py-3">
          <div>
            <Money formatted={quote.data.amount.formatted} size="xl" tone="ok" className="block" />
            <div className="mt-1 text-[12.5px] font-semibold text-ink-2">
              <span data-testid="quote-amount">{quote.data.amount.formatted}</span> · {t("session.amountNow")} · {t("session.recalc")}
            </div>
          </div>
          <ErrorBox error={prepay.error} />
        </div>
      )}

      {/* Деньги — отдельной строкой на всю ширину (сумма не обрезается), ниже — открыть выезд и исправить номер */}
      <div className="mt-auto flex flex-col gap-2">
        {canCash && pending ? (
          <Button variant="money" size="lg" className="w-full" disabled={confirmCash.isPending} onClick={() => confirmCash.mutate({ paymentId: pending.id, amountMinor: pending.amount_minor }, { onSuccess: onPaid })} data-testid="confirm-cash">
            {t("session.payCash")} · {pending.amount.formatted}
          </Button>
        ) : canCash && quote.data && isActive ? (
          <Button variant="money" size="lg" className="w-full" disabled={prepay.isPending} title={t("session.prepayHint")} onClick={() => prepay.mutate({ amount_minor: quote.data.amount_minor }, { onSuccess: onPaid })} data-testid="prepay-cash">
            {t("session.prepay")} · {quote.data.amount.formatted}
          </Button>
        ) : null}
        <div className="grid grid-cols-2 gap-2 [&>button]:whitespace-normal [&>button]:leading-tight">
          {canOpen && isActive && (
            <Button variant="default" size="lg" onClick={() => setOpening(true)} data-testid="open-exit">
              {t("session.openExit")}
            </Button>
          )}
          {canWrite && isActive && (
            <Button variant="outline" size="lg" onClick={() => setCorrecting(true)} data-testid="correct-plate">
              {t("session.correct")}
            </Button>
          )}
        </div>
      </div>

      <Dialog open={correcting} onClose={() => setCorrecting(false)} title={t("session.correctTitle")} initialFocus={plateInput}>
        <p className="mb-3 flex flex-wrap items-center gap-2 text-[13.5px] font-semibold">
          {t("session.recognized")}:
          <span data-testid="original-plate">
            <Plate plate={s.plate} size="sm" />
          </span>
          <span className="text-muted">— {t("session.orig")}</span>
        </p>
        <ReasonForm
          testId="correct"
          busy={correct.isPending}
          submitLabel={t("session.save")}
          onSubmit={(reason) =>
            isPlateValid(newPlate) &&
            correct.mutate(
              { plate: newPlate, reason },
              {
                onSuccess: () => {
                  setCorrecting(false);
                  toast.ok(t("session.corrected"));
                },
              },
            )
          }
        >
          <div>
            <PlateInput id="new-plate" ref={plateInput} value={newPlate} onChange={setNewPlate} label={t("session.newPlate")} size="md" testId="new-plate" />
          </div>
          <ErrorBox error={correct.error} />
        </ReasonForm>
      </Dialog>

      <Dialog open={opening} onClose={() => setOpening(false)} title={t("session.openExitTitle")}>
        <ReasonForm
          testId="open-exit"
          busy={openExit.isPending}
          submitLabel={t("session.openConfirm")}
          onSubmit={(reason) =>
            openExit.mutate(
              { reason },
              {
                onSuccess: () => {
                  setOpening(false);
                  toast.ok(t("session.opened"));
                },
              },
            )
          }
        >
          <ErrorBox error={openExit.error} />
        </ReasonForm>
      </Dialog>
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-surface-2 px-3 py-2">
      <div className="text-[10.5px] font-bold uppercase tracking-[0.08em] text-muted">{label}</div>
      <div className="text-[15px] font-extrabold">{value}</div>
    </div>
  );
}

/** Марка/модель/цвет под номером: видно сразу, правится карандашом прямо в карточке (номер вводить не нужно). */
function VehicleLine({ sessionId, make, model, color, canEdit }: { sessionId: string; make?: string | null; model?: string | null; color?: string | null; canEdit: boolean }) {
  const { t } = useTranslation();
  const save = useSetSessionVehicle(sessionId);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState<VehicleValue>({ make: make ?? "", model: model ?? "", color: color ?? "" });
  const label = vehicleLabel(t, make, model, color);
  const sw = colorSwatch(color);
  return (
    <>
      <div className="flex items-center gap-2 text-[14px] font-extrabold" data-testid="session-vehicle">
        {sw ? <i aria-hidden="true" className={cn("h-3 w-3 rounded-full border border-black/15", sw)} /> : <Car className="h-4 w-4 text-muted" aria-hidden="true" />}
        {label ?? <span className="font-semibold text-muted">{t("vehicle.unknown")}</span>}
        {canEdit && (
          <button
            type="button"
            onClick={() => {
              setValue({ make: make ?? "", model: model ?? "", color: color ?? "" });
              setEditing(true);
            }}
            className="rounded-md p-1 text-accent-ink hover:bg-accent-soft"
            aria-label={label ? t("vehicle.edit") : t("vehicle.add")}
            title={label ? t("vehicle.edit") : t("vehicle.add")}
            data-testid="edit-vehicle"
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        )}
      </div>
      <Dialog open={editing} onClose={() => setEditing(false)} title={t("vehicle.dialogTitle")}>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(
              { make: value.make.trim() || null, model: value.model.trim() || null, color: value.color || null, reason: t("vehicle.reason") },
              {
                onSuccess: () => {
                  toast.ok(t("vehicle.saved"));
                  setEditing(false);
                },
              },
            );
          }}
        >
          <VehicleFields value={value} onChange={setValue} />
          <ErrorBox error={save.error} />
          <Button type="submit" size="lg" disabled={save.isPending} data-testid="vehicle-save">
            {t("session.save")}
          </Button>
        </form>
      </Dialog>
    </>
  );
}
