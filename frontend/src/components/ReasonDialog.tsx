import { useState, type ReactNode } from "react";
import { useTranslation } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Dialog } from "@/components/ui/dialog";
import { ErrorBox } from "@/components/ErrorBox";

/* Основание ручного действия (I-15) — своим диалогом, не window.prompt(): prompt не работает во встроенных
   браузерах (Electron, киоск-режим, WebView) и не проходит проверку длины до отправки. */
export function ReasonDialog({
  open,
  title,
  confirm,
  busy,
  danger = true,
  defaultReason = "",
  error,
  onClose,
  onConfirm,
}: {
  open: boolean;
  title: string;
  confirm: string;
  busy: boolean;
  danger?: boolean;
  defaultReason?: string;
  error?: unknown;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}) {
  return (
    <Dialog open={open} onClose={onClose} title={title}>
      {/* форма монтируется заново на каждое открытие: основание прошлого действия не подставляется */}
      {open && <ReasonForm confirm={confirm} busy={busy} danger={danger} defaultReason={defaultReason} error={error} onClose={onClose} onConfirm={onConfirm} />}
    </Dialog>
  );
}

function ReasonForm({ confirm, busy, danger, defaultReason, error, onClose, onConfirm }: { confirm: string; busy: boolean; danger: boolean; defaultReason: string; error: unknown; onClose: () => void; onConfirm: (reason: string) => void }) {
  const { t } = useTranslation();
  const [reason, setReason] = useState(defaultReason);
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy && reason.trim().length >= 3) onConfirm(reason.trim());
      }}
    >
      <Label htmlFor="reason">{t("common.reason")}</Label>
      <Input id="reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("common.reasonPh")} data-testid="reason-input" />
      {error != null && <ErrorBox error={error} />}
      <div className="flex gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" variant={danger ? "danger" : "default"} className="ml-auto" disabled={busy || reason.trim().length < 3} data-testid="reason-confirm">
          {confirm}
        </Button>
      </div>
    </form>
  );
}

interface Ask {
  title: string;
  confirm: string;
  danger?: boolean;
  defaultReason?: string;
  /** действие с основанием; Promise — окно ждёт ответа: успех закрывает, ошибка показывается внутри */
  run: (reason: string) => Promise<unknown> | void;
}

/** Замена window.prompt для основания: `const [dialog, ask] = useReasonDialog(); … ask({...}); return <>{dialog}…</>`. */
// eslint-disable-next-line react-refresh/only-export-components -- хук живёт рядом со своим диалогом
export function useReasonDialog(busy = false): [ReactNode, (ask: Ask) => void] {
  const [ask, setAsk] = useState<Ask | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const open = (next: Ask) => {
    setError(null);
    setAsk(next);
  };
  const dialog = ask ? (
    <ReasonDialog
      key={ask.title}
      open
      title={ask.title}
      confirm={ask.confirm}
      danger={ask.danger}
      defaultReason={ask.defaultReason}
      busy={busy || pending}
      error={error}
      onClose={() => {
        if (!pending) setAsk(null);
      }}
      onConfirm={async (reason) => {
        setPending(true);
        setError(null);
        try {
          await ask.run(reason);
          setAsk(null);
        } catch (e) {
          setError(e); // окно остаётся открытым: видно, почему не получилось, можно повторить
        } finally {
          setPending(false);
        }
      }}
    />
  ) : null;
  return [dialog, open];
}
