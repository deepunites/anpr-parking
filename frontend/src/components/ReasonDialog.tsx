import { useState, type ReactNode } from "react";
import { useTranslation } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Dialog } from "@/components/ui/dialog";

/* Основание ручного действия (I-15) — своим диалогом, не window.prompt(): prompt не работает во встроенных
   браузерах (Electron, киоск-режим, WebView) и не проходит проверку длины до отправки. */
export function ReasonDialog({
  open,
  title,
  confirm,
  busy,
  danger = true,
  defaultReason = "",
  onClose,
  onConfirm,
}: {
  open: boolean;
  title: string;
  confirm: string;
  busy: boolean;
  danger?: boolean;
  defaultReason?: string;
  onClose: () => void;
  onConfirm: (reason: string) => void;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState(defaultReason);
  return (
    <Dialog open={open} onClose={onClose} title={title}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          onConfirm(reason.trim());
        }}
      >
        <Label htmlFor="reason">{t("common.reason")}</Label>
        <Input id="reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("common.reasonPh")} data-testid="reason-input" />
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" variant={danger ? "danger" : "default"} className="ml-auto" disabled={busy || reason.trim().length < 3} data-testid="reason-confirm">
            {confirm}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

interface Ask {
  title: string;
  confirm: string;
  danger?: boolean;
  defaultReason?: string;
  run: (reason: string) => void;
}

/** Замена window.prompt для основания: `const [dialog, ask] = useReasonDialog(); … ask({...}); return <>{dialog}…</>`. */
// eslint-disable-next-line react-refresh/only-export-components -- хук живёт рядом со своим диалогом
export function useReasonDialog(busy = false): [ReactNode, (ask: Ask) => void] {
  const [ask, setAsk] = useState<Ask | null>(null);
  const dialog = ask ? (
    <ReasonDialog
      key={ask.title}
      open
      title={ask.title}
      confirm={ask.confirm}
      danger={ask.danger}
      defaultReason={ask.defaultReason}
      busy={busy}
      onClose={() => setAsk(null)}
      onConfirm={(reason) => {
        ask.run(reason);
        setAsk(null);
      }}
    />
  ) : null;
  return [dialog, setAsk];
}
