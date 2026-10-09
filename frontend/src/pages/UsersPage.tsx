import { useMemo, useState } from "react";
import { Copy, KeyRound, Link2, Plus, UserCog } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import { useAuth } from "@/auth/useAuth";
import { hasPermission } from "@/auth/store";
import { useChangeRole, useCreateUser, useDeactivateUser, useInvites, useLots, useResetPassword, useRevokeInvite, useUpdateUser, useUsers, type InviteOut, type Role, type UserOut } from "@/api/hooks";
import { InviteForm, InviteLinkView, type IssuedInvite } from "@/components/staff/InviteForm";
import { Badge } from "@/components/ui/badge";
import { formatPhone, isEmail, normalizePhone } from "@/lib/phone";
import { Card, Eyebrow } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { StatusDot } from "@/components/ui/badge";
import { Drawer } from "@/components/ui/drawer";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { Tabs } from "@/components/ui/tabs";
import { cn } from "@/lib/cn";
import { copyToClipboard } from "@/lib/clipboard";

/* Роли, которые администратор клиента выдаёт сотрудникам; PLATFORM_* — только у оператора платформы. */
const STAFF_ROLES: Role[] = ["OPERATOR", "CASHIER", "MANAGER", "VIEWER", "DEVELOPER", "OWNER", "TENANT_ADMIN"];

/** Временный пароль: 16 символов без похожих (0/O, 1/l), из криптографического генератора браузера. */
function tempPassword(): string {
  const alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

function initials(name: string): string {
  return name
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
}

/* Сотрудники: приглашение по ссылке (48 ч, пароль сотрудник придумывает сам) или добавление с временным паролем
   (сменит при первом входе); логин — e-mail и/или телефон; редактирование имени, логинов, площадок, роли;
   отключение/включение, сброс пароля. */

const loginOf = (u: { email?: string | null; phone?: string | null }) => u.email || formatPhone(u.phone) || "";
export function UsersPage() {
  const { t } = useTranslation();
  const { me } = useAuth();
  const canWrite = hasPermission(me, "users:write");
  const users = useUsers(true);
  const [filter, setFilter] = useState<"all" | Role | "inactive">("all");
  const [adding, setAdding] = useState(false);
  const [inviting, setInviting] = useState(false);
  const [issuedInvite, setIssuedInvite] = useState<IssuedInvite | null>(null);
  const [editing, setEditing] = useState<UserOut | null>(null);
  const [issued, setIssued] = useState<{ user: UserOut; password: string; kind: "created" | "reset" } | null>(null);

  const items = useMemo(() => users.data?.items ?? [], [users.data]);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length, inactive: items.filter((u) => !u.active).length };
    for (const u of items) c[u.role] = (c[u.role] ?? 0) + 1;
    return c;
  }, [items]);
  const visible = items.filter((u) => (filter === "all" ? true : filter === "inactive" ? !u.active : u.role === filter && u.active));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.users")}</h1>
        {canWrite && (
          <div className="ml-auto flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setAdding(true)} data-testid="add-user">
              <Plus className="h-4 w-4" aria-hidden="true" /> {t("users.addWithPassword")}
            </Button>
            <Button onClick={() => setInviting(true)} data-testid="invite-user">
              <Link2 className="h-4 w-4" aria-hidden="true" /> {t("users.invite")}
            </Button>
          </div>
        )}
      </div>
      <div className="max-w-3xl">
        <Tabs
          items={[
            { id: "all", label: t("users.all"), count: counts["all"] },
            { id: "OPERATOR", label: t("roles.OPERATOR"), count: counts["OPERATOR"] ?? 0 },
            { id: "CASHIER", label: t("roles.CASHIER"), count: counts["CASHIER"] ?? 0 },
            { id: "MANAGER", label: t("roles.MANAGER"), count: counts["MANAGER"] ?? 0 },
            { id: "inactive", label: t("users.inactive"), count: counts["inactive"] },
          ]}
          value={filter}
          onChange={(id) => setFilter(id as typeof filter)}
        />
      </div>
      <Card>
        {users.isPending && <SkeletonRows />}
        {users.isError && <ErrorBox error={users.error} onRetry={() => void users.refetch()} />}
        {users.data && visible.length === 0 && <EmptyState title={t("users.empty")} hint={canWrite ? t("users.emptyHint") : undefined} />}
        {users.data && visible.length > 0 && (
          <Table data-testid="users-table">
            <thead>
              <tr>
                <Th>{t("users.employee")}</Th>
                <Th>{t("common.role")}</Th>
                <Th>{t("users.lots")}</Th>
                <Th>{t("session.status")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {visible.map((u) => (
                <UserRow key={u.id} user={u} isSelf={u.id === me?.user_id} canWrite={canWrite} onEdit={() => setEditing(u)} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {canWrite && <InvitesCard />}

      <Drawer
        open={inviting}
        onClose={() => {
          setInviting(false);
          setIssuedInvite(null);
        }}
        title={issuedInvite ? t("users.inviteCreated") : t("users.invite")}
      >
        {issuedInvite ? (
          <InviteLinkView
            issued={issuedInvite}
            onDone={() => {
              setInviting(false);
              setIssuedInvite(null);
            }}
          />
        ) : (
          <InviteForm
            roles={STAFF_ROLES}
            onIssued={setIssuedInvite}
            renderRole={(value, onChange) => <RolePicker value={value} onChange={onChange} />}
            renderLots={(value, onChange) => <LotScopePicker value={value} onChange={onChange} />}
          />
        )}
      </Drawer>

      <Drawer open={adding} onClose={() => setAdding(false)} title={t("users.addWithPassword")}>
        <AddUserForm
          onDone={(user, password) => {
            setAdding(false);
            setIssued({ user, password, kind: "created" });
          }}
        />
      </Drawer>

      <Drawer open={editing !== null} onClose={() => setEditing(null)} title={editing ? editing.display_name || loginOf(editing) : ""}>
        {editing && <EditUserForm user={editing} isSelf={editing.id === me?.user_id} onDone={() => setEditing(null)} onPasswordReset={(password) => setIssued({ user: editing, password, kind: "reset" })} />}
      </Drawer>

      <Dialog open={issued !== null} onClose={() => setIssued(null)} title={issued?.kind === "created" ? t("users.createdTitle") : t("users.resetTitle")}>
        {issued && <IssuedPasswordView user={issued.user} password={issued.password} onClose={() => setIssued(null)} />}
      </Dialog>
    </div>
  );
}

function UserRow({ user: u, isSelf, canWrite, onEdit }: { user: UserOut; isSelf: boolean; canWrite: boolean; onEdit: () => void }) {
  const { t } = useTranslation();
  const lots = useLots();
  const scope = u.lot_scope.length === 0 ? t("users.allLots") : u.lot_scope.map((id) => lots.data?.find((l) => l.id === id)?.name ?? id.slice(0, 8)).join(", ");
  return (
    <tr data-testid="user-row" data-email={u.email ?? u.phone ?? ""} className={cn(!u.active && "opacity-60")}>
      <Td>
        <div className="flex items-center gap-2.5">
          <span className="grid h-8 w-8 flex-none place-items-center rounded-full bg-accent-soft text-[11px] font-extrabold text-accent-ink" aria-hidden="true">
            {initials(u.display_name || loginOf(u))}
          </span>
          <span className="min-w-0">
            <b className="block truncate">
              {u.display_name || loginOf(u)}
              {isSelf && <span className="ml-1.5 text-[11px] font-bold text-muted">· {t("users.you")}</span>}
            </b>
            <span className="block truncate text-[12px] font-semibold text-muted">{[u.email, formatPhone(u.phone)].filter(Boolean).join(" · ")}</span>
            {u.must_change_password && (
              <span className="text-[11.5px] font-bold text-warn-ink" data-testid="must-change">
                {t("users.mustChange")}
              </span>
            )}
          </span>
        </div>
      </Td>
      <Td>{t(`roles.${u.role}`, { defaultValue: u.role })}</Td>
      <Td className="text-[12.5px]">{scope}</Td>
      <Td>
        <StatusDot tone={u.active ? "green" : "red"}>{u.active ? t("common.active") : t("users.disabled")}</StatusDot>
      </Td>
      <Td>
        {canWrite && (
          <Button variant="outline" size="sm" onClick={onEdit} data-testid="edit-user">
            <UserCog className="h-4 w-4" aria-hidden="true" /> {t("users.manage")}
          </Button>
        )}
      </Td>
    </tr>
  );
}

function RolePicker({ value, onChange, exclude }: { value: Role; onChange: (r: Role) => void; exclude?: Role[] }) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-2" role="radiogroup" aria-label={t("common.role")}>
      {STAFF_ROLES.filter((r) => !exclude?.includes(r)).map((r) => (
        <button
          key={r}
          type="button"
          role="radio"
          aria-checked={value === r}
          onClick={() => onChange(r)}
          className={cn("grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-0.5 rounded-xl border-[1.5px] px-3 py-2.5 text-left", value === r ? "border-accent bg-accent-soft/60" : "border-line hover:bg-surface-2")}
          data-testid={`role-${r}`}
        >
          <span aria-hidden="true" className={cn("row-span-2 h-4 w-4 rounded-full border-2", value === r ? "border-accent bg-accent shadow-[inset_0_0_0_3px_var(--surface)]" : "border-line")} />
          <span className="text-[13.5px] font-bold">{t(`roles.${r}`)}</span>
          <span className="text-[12px] font-semibold text-muted">{t(`roleHint.${r}`)}</span>
        </button>
      ))}
    </div>
  );
}

function LotScopePicker({ value, onChange }: { value: string[]; onChange: (ids: string[]) => void }) {
  const { t } = useTranslation();
  const lots = useLots();
  const all = value.length === 0;
  return (
    <div className="flex flex-wrap gap-2">
      <button type="button" onClick={() => onChange([])} className={cn("rounded-full border-[1.5px] px-3 py-1.5 text-[12.5px] font-bold", all ? "border-accent bg-accent-soft text-accent-ink" : "border-line")} aria-pressed={all}>
        {t("users.allLots")}
      </button>
      {(lots.data ?? []).map((l) => {
        const on = value.includes(l.id);
        return (
          <button
            key={l.id}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((id) => id !== l.id) : [...value, l.id])}
            className={cn("rounded-full border-[1.5px] px-3 py-1.5 text-[12.5px] font-bold", on ? "border-accent bg-accent-soft text-accent-ink" : "border-line")}
          >
            {l.name}
          </button>
        );
      })}
    </div>
  );
}

function AddUserForm({ onDone }: { onDone: (user: UserOut, password: string) => void }) {
  const { t, i18n } = useTranslation();
  const create = useCreateUser();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [role, setRole] = useState<Role>("OPERATOR");
  const [lotScope, setLotScope] = useState<string[]>([]);
  const emailOk = email.trim() === "" || isEmail(email);
  const phoneNorm = normalizePhone(phone);
  const phoneOk = phone.trim() === "" || phoneNorm !== null;
  const valid = name.trim().length >= 2 && emailOk && phoneOk && (email.trim() !== "" || phoneNorm !== null);
  return (
    <form
      className="flex flex-col gap-1"
      onSubmit={(e) => {
        e.preventDefault();
        if (!valid) return;
        const password = tempPassword();
        create.mutate(
          { display_name: name.trim(), email: email.trim().toLowerCase() || null, phone: phoneNorm, password, role, lot_scope: lotScope, locale: i18n.language, reason: t("users.createReason") },
          {
            onSuccess: (u) => {
              toast.ok(`${t("users.created")} · ${u.display_name}`);
              onDone(u, password);
            },
          },
        );
      }}
      data-testid="user-form"
    >
      <Label htmlFor="u-name">{t("users.fullName")}</Label>
      <Input id="u-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("users.fullNamePh")} data-testid="user-name" autoFocus />
      <Label htmlFor="u-email" className="mt-3">
        {t("common.email")}
      </Label>
      <Input id="u-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ism.familiya@company.uz" data-testid="user-email" />
      <Label htmlFor="u-phone" className="mt-3">
        {t("users.phone")}
      </Label>
      <Input id="u-phone" type="tel" inputMode="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder={t("users.phonePh")} data-testid="user-phone" />
      <p className={cn("text-[12px] font-semibold", emailOk && phoneOk ? "text-muted" : "text-warn-ink")}>
        {!emailOk ? t("error.codes.INVALID_EMAIL") : !phoneOk ? t("error.codes.INVALID_PHONE") : t("users.loginHint")}
      </p>
      <Eyebrow className="mt-3 mb-1">{t("users.roleWhat")}</Eyebrow>
      <RolePicker value={role} onChange={setRole} />
      <Eyebrow className="mt-3 mb-1">{t("users.lots")}</Eyebrow>
      <LotScopePicker value={lotScope} onChange={setLotScope} />
      <div className="mt-4 rounded-xl bg-surface-2 p-3 text-[12.5px] font-semibold text-ink-2">{t("users.tempHint")}</div>
      {create.isError && <ErrorBox error={create.error} />}
      <div className="mt-4 flex">
        <Button type="submit" className="ml-auto" disabled={!valid || create.isPending} data-testid="user-submit">
          <KeyRound className="h-4 w-4" aria-hidden="true" /> {t("users.createWithPassword")}
        </Button>
      </div>
    </form>
  );
}

function EditUserForm({ user, isSelf, onDone, onPasswordReset }: { user: UserOut; isSelf: boolean; onDone: () => void; onPasswordReset: (password: string) => void }) {
  const { t } = useTranslation();
  const update = useUpdateUser();
  const changeRole = useChangeRole();
  const deactivate = useDeactivateUser();
  const reset = useResetPassword();
  const [name, setName] = useState(user.display_name);
  const [email, setEmail] = useState(user.email ?? "");
  const [phone, setPhone] = useState(formatPhone(user.phone));
  const [lotScope, setLotScope] = useState<string[]>(user.lot_scope);
  const [role, setRole] = useState<Role>(user.role);
  const [reason, setReason] = useState("");
  const reasonOk = reason.trim().length >= 3;
  const busy = update.isPending || changeRole.isPending || deactivate.isPending || reset.isPending;

  const phoneNorm = normalizePhone(phone);
  const loginsOk = (email.trim() === "" || isEmail(email)) && (phone.trim() === "" || phoneNorm !== null) && (email.trim() !== "" || phoneNorm !== null);
  const emailChanged = email.trim().toLowerCase() !== (user.email ?? "");
  const phoneChanged = (phoneNorm ?? "") !== (user.phone ?? "");

  const save = async () => {
    const changedProfile = name.trim() !== user.display_name || lotScope.join() !== user.lot_scope.join() || emailChanged || phoneChanged;
    if (changedProfile)
      await update.mutateAsync({
        userId: user.id,
        body: {
          display_name: name.trim(),
          lot_scope: lotScope,
          reason: reason.trim(),
          ...(emailChanged ? { email: email.trim().toLowerCase() } : {}),
          ...(phoneChanged ? { phone: phoneNorm ?? "" } : {}),
        },
      });
    if (role !== user.role) await changeRole.mutateAsync({ userId: user.id, role, reason: reason.trim() });
    toast.ok(t("users.saved"));
    onDone();
  };

  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor="e-name">{t("users.fullName")}</Label>
      <Input id="e-name" value={name} onChange={(e) => setName(e.target.value)} data-testid="edit-name" />
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <Label htmlFor="e-email">{t("common.email")}</Label>
          <Input id="e-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} data-testid="edit-email" />
        </div>
        <div>
          <Label htmlFor="e-phone">{t("users.phone")}</Label>
          <Input id="e-phone" type="tel" inputMode="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder={t("users.phonePh")} data-testid="edit-phone" />
        </div>
      </div>
      {!loginsOk && <p className="text-[12px] font-semibold text-warn-ink">{t("users.loginHint")}</p>}
      <Eyebrow className="mt-3 mb-1">{t("users.roleWhat")}</Eyebrow>
      {isSelf ? <p className="text-[12.5px] font-semibold text-muted">{t("users.selfRole")}</p> : <RolePicker value={role} onChange={setRole} />}
      <Eyebrow className="mt-3 mb-1">{t("users.lots")}</Eyebrow>
      <LotScopePicker value={lotScope} onChange={setLotScope} />
      <Label htmlFor="e-reason" className="mt-3">
        {t("session.reason")}
      </Label>
      <Input id="e-reason" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("users.reasonPh")} data-testid="edit-reason" />
      {!reasonOk && <p className="text-[12px] font-semibold text-muted">{t("session.reasonRequired")}</p>}
      {(update.isError || changeRole.isError) && <ErrorBox error={update.error ?? changeRole.error} />}
      <div className="mt-3 flex gap-2">
        <Button type="button" className="ml-auto" disabled={!reasonOk || !loginsOk || busy} onClick={() => void save()} data-testid="edit-save">
          {t("session.save")}
        </Button>
      </div>

      <Eyebrow className="mt-6 mb-2">{t("users.dangerZone")}</Eyebrow>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={!reasonOk || busy}
          onClick={() => {
            const password = tempPassword();
            reset.mutate({ userId: user.id, password, reason: reason.trim() }, { onSuccess: () => onPasswordReset(password) });
          }}
          data-testid="reset-password"
        >
          <KeyRound className="h-4 w-4" aria-hidden="true" /> {t("users.resetPassword")}
        </Button>
        {!isSelf && user.active && (
          <Button type="button" variant="danger" disabled={!reasonOk || busy} onClick={() => deactivate.mutate({ userId: user.id, reason: reason.trim() }, { onSuccess: () => { toast.ok(t("users.disabledToast")); onDone(); } })} data-testid="deactivate-user">
            {t("users.deactivate")}
          </Button>
        )}
        {!user.active && (
          <Button type="button" variant="money" disabled={!reasonOk || busy} onClick={() => update.mutate({ userId: user.id, body: { active: true, reason: reason.trim() } }, { onSuccess: () => { toast.ok(t("users.enabledToast")); onDone(); } })} data-testid="reactivate-user">
            {t("users.reactivate")}
          </Button>
        )}
      </div>
      <p className="mt-2 text-[12px] font-semibold text-muted">{t("users.dangerHint")}</p>
      {(deactivate.isError || reset.isError) && <ErrorBox error={deactivate.error ?? reset.error} />}
    </div>
  );
}

function IssuedPasswordView({ user, password, onClose }: { user: UserOut; password: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await copyToClipboard(`${window.location.origin}/login\n${loginOf(user)}\n${password}`);
      setCopied(true);
      toast.ok(t("devices.copied"));
    } catch {
      toast.warn(t("devices.copyFailed"));
    }
  };
  return (
    <div className="space-y-3" data-testid="issued-password">
      <p className="text-[14px] font-semibold text-ink-2">{t("users.issuedHint")}</p>
      <div className="grid gap-2 rounded-xl bg-surface-2 p-3 text-[13.5px] font-semibold">
        <div>
          <Eyebrow>{t("users.loginAt")}</Eyebrow>
          <span className="font-mono">{window.location.origin}/login</span>
        </div>
        <div>
          <Eyebrow>{t("login.email")}</Eyebrow>
          <span className="font-mono">{loginOf(user)}</span>
        </div>
        <div>
          <Eyebrow>{t("users.tempPassword")}</Eyebrow>
          <span className="font-mono text-[18px] font-bold tracking-wide" data-testid="temp-password">
            {password}
          </span>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={() => void copy()} data-testid="copy-password">
          <Copy className="h-4 w-4" aria-hidden="true" /> {copied ? t("devices.copied") : t("users.copyAll")}
        </Button>
        <Button className="ml-auto" onClick={onClose} data-testid="password-done">
          {t("users.handedOver")}
        </Button>
      </div>
    </div>
  );
}

function InvitesCard() {
  const { t, i18n } = useTranslation();
  const invites = useInvites(true);
  const revoke = useRevokeInvite();
  const items = (invites.data ?? []).filter((i: InviteOut) => i.status !== "ACCEPTED").slice(0, 20);
  if (items.length === 0) return null;
  const tone = { PENDING: "blue", EXPIRED: "gray", REVOKED: "gray", ACCEPTED: "green" } as const;
  const date = (iso: string) => new Date(iso).toLocaleString(i18n.language === "uz" ? "uz-UZ" : "ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  return (
    <Card data-testid="invites-card">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-[15px] font-extrabold">{t("users.invites")}</h2>
        <span className="text-[12px] font-semibold text-muted">{t("users.invitesHint")}</span>
      </div>
      <ul className="divide-y divide-line">
        {items.map((i) => (
          <li key={i.id} className="flex flex-wrap items-center gap-2 py-2" data-testid="invite-row" data-status={i.status}>
            <b className="text-[13.5px]">{i.display_name}</b>
            <span className="text-[12.5px] font-semibold text-muted">
              {loginOf(i)} · {t(`roles.${i.role}`)}
            </span>
            <Badge tone={tone[i.status as keyof typeof tone] ?? "gray"}>{t(`users.inviteStatus.${i.status}`)}</Badge>
            {i.status === "PENDING" && <span className="text-[12px] font-semibold text-muted">{fmt(t("users.until"), { date: date(i.expires_at) })}</span>}
            {i.status === "PENDING" && (
              <Button size="sm" variant="ghost" className="ml-auto" disabled={revoke.isPending} onClick={() => revoke.mutate({ inviteId: i.id, reason: t("users.revokeReason") }, { onSuccess: () => toast.ok(t("users.inviteStatus.REVOKED")) })} data-testid="revoke-invite">
                {t("users.revoke")}
              </Button>
            )}
          </li>
        ))}
      </ul>
      {revoke.isError && <ErrorBox error={revoke.error} />}
    </Card>
  );
}
