import { useState } from "react";
import { Check, Copy, Link2, Send } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { useCreateInvite, type InviteOut, type Role } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ErrorBox } from "@/components/ErrorBox";
import { isEmail, normalizePhone } from "@/lib/phone";
import { cn } from "@/lib/cn";

export interface IssuedInvite {
  invite: InviteOut;
  link: string;
}

/** Приглашение по ссылке: имя, телефон или e-mail, роль, площадки. Ссылку показываем один раз (в БД — только хеш). */
export function InviteForm({
  roles,
  onIssued,
  renderRole,
  renderLots,
  compact,
}: {
  roles: Role[];
  onIssued: (issued: IssuedInvite) => void;
  renderRole?: (value: Role, onChange: (r: Role) => void) => React.ReactNode;
  renderLots?: (value: string[], onChange: (ids: string[]) => void) => React.ReactNode;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const create = useCreateInvite();
  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [role, setRole] = useState<Role>(roles[0]!);
  const [lots, setLots] = useState<string[]>([]);
  const raw = contact.trim();
  const byEmail = raw.includes("@");
  const phone = byEmail ? null : normalizePhone(raw);
  const contactOk = byEmail ? isEmail(raw) : phone !== null;
  const valid = name.trim().length >= 2 && contactOk;

  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid || create.isPending) return;
        create.mutate(
          {
            display_name: name.trim(),
            role,
            email: byEmail ? raw.toLowerCase() : null,
            phone: byEmail ? null : phone,
            lot_scope: lots,
            reason: t("users.inviteReason"),
          },
          { onSuccess: (r) => onIssued({ invite: r.invite, link: `${window.location.origin}/join/${r.token}` }) },
        );
      }}
      data-testid="invite-form"
    >
      <div className={cn("grid gap-3", compact && "sm:grid-cols-2")}>
        <div>
          <Label htmlFor="inv-name">{t("users.fullName")}</Label>
          <Input id="inv-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("users.fullNamePh")} data-testid="invite-name" autoFocus={!compact} />
        </div>
        <div>
          <Label htmlFor="inv-contact">{t("login.email")}</Label>
          <Input id="inv-contact" value={contact} onChange={(e) => setContact(e.target.value)} placeholder={t("users.phonePh")} inputMode={byEmail ? "email" : "tel"} data-testid="invite-contact" />
          {raw && !contactOk && <p className="mt-1 text-[12px] font-semibold text-warn-ink">{byEmail ? t("error.codes.INVALID_EMAIL") : t("error.codes.INVALID_PHONE")}</p>}
        </div>
      </div>
      <div className="mt-3 mb-1 text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t("users.roleWhat")}</div>
      {renderRole ? (
        renderRole(role, setRole)
      ) : (
        <div className="flex flex-wrap gap-2">
          {roles.map((r) => (
            <button key={r} type="button" onClick={() => setRole(r)} aria-pressed={role === r} className={cn("rounded-full border px-3 py-1.5 text-[13px] font-bold", role === r ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} data-testid={`invite-role-${r}`}>
              {t(`roles.${r}`)}
            </button>
          ))}
        </div>
      )}
      {renderLots && (
        <>
          <div className="mt-3 mb-1 text-[11px] font-bold uppercase tracking-[0.08em] text-muted">{t("users.lots")}</div>
          {renderLots(lots, setLots)}
        </>
      )}
      {create.isError && <ErrorBox error={create.error} />}
      <div className="mt-4 flex">
        <Button type="submit" className="ml-auto" disabled={!valid || create.isPending} data-testid="invite-submit">
          <Link2 className="h-4 w-4" aria-hidden="true" /> {t("users.createInvite")}
        </Button>
      </div>
    </form>
  );
}

/** Ссылка приглашения: копирование и отправка через Telegram/WhatsApp/SMS — пока канал D-3 не подключён (A-76). */
export function InviteLinkView({ issued, onDone, doneLabel }: { issued: IssuedInvite; onDone: () => void; doneLabel?: string }) {
  const { t, i18n } = useTranslation();
  const { me } = useAuth();
  const [copied, setCopied] = useState(false);
  const text = fmt(t("users.shareText"), { company: me?.tenant_name ?? "", link: issued.link });
  const until = new Date(issued.invite.expires_at).toLocaleString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  const phone = issued.invite.phone?.replace("+", "") ?? "";
  const share = [
    { id: "telegram", label: "Telegram", href: `https://t.me/share/url?url=${encodeURIComponent(issued.link)}&text=${encodeURIComponent(text.replace(issued.link, "").trim())}` },
    { id: "whatsapp", label: "WhatsApp", href: `https://wa.me/${phone}?text=${encodeURIComponent(text)}` },
    { id: "sms", label: "SMS", href: `sms:${issued.invite.phone ?? ""}?body=${encodeURIComponent(text)}` },
  ];
  return (
    <div className="space-y-3" data-testid="invite-issued">
      <p className="text-[13px] font-semibold text-ink-2">{t("users.inviteHint")}</p>
      <div className="rounded-xl border border-line bg-surface-2 p-3">
        <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.08em] text-muted">
          {t("users.link")} · {issued.invite.display_name} · {fmt(t("users.until"), { date: until })}
        </div>
        <code className="block break-all font-mono text-[12.5px]" data-testid="invite-link">
          {issued.link}
        </code>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(issued.link);
              setCopied(true);
            } catch {
              /* буфер недоступен — ссылка видна текстом */
            }
          }}
          data-testid="copy-invite-link"
        >
          {copied ? <Check className="h-4 w-4" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />} {copied ? t("users.copied") : t("users.copyLink")}
        </Button>
        <span className="text-[12px] font-bold text-muted">{t("users.sendVia")}:</span>
        {share.map((s) => (
          <a key={s.id} href={s.href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1.5 text-[12.5px] font-bold hover:bg-surface-2">
            <Send className="h-3.5 w-3.5" aria-hidden="true" /> {s.label}
          </a>
        ))}
      </div>
      <div className="flex">
        <Button className="ml-auto" onClick={onDone} data-testid="invite-done">
          {doneLabel ?? t("users.handedOver")}
        </Button>
      </div>
    </div>
  );
}
