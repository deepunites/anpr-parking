import { Fragment, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Copy, Inbox, KeyRound, Plus, RefreshCw, Send, Trash2, Webhook } from "lucide-react";
import { fmt, useTranslation } from "@/i18n";
import {
  useApiKeys,
  useCreateApiKey,
  useCreateInbox,
  useCreateWebhook,
  useDeleteWebhook,
  useDeliveries,
  useInbox,
  useIntegrationsCatalog,
  useRedeliver,
  useRevokeApiKey,
  useRotateWebhookSecret,
  useTestWebhook,
  useUpdateWebhook,
  useWebhooks,
  type ApiKeyOut,
  type WebhookOut,
} from "@/api/hooks";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import { Card, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Segmented, Select } from "@/components/ui/input";
import { Table, Td, Th } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Drawer } from "@/components/ui/drawer";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";
import { toast } from "@/components/ui/toast";
import { Tabs } from "@/components/ui/tabs";
import { ReasonDialog } from "@/components/ReasonDialog";
import { JournalTab } from "@/components/integrations/JournalTab";
import { SandboxTab } from "@/components/integrations/SandboxTab";
import { DocsTab } from "@/components/integrations/DocsTab";

const TABS = ["keys", "webhooks", "journal", "sandbox", "docs"] as const;
type Tab = (typeof TABS)[number];
const INBOX_KEY = "anpr.test-inbox";

async function copyText(text: string, done: string, failed: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.ok(done);
  } catch {
    toast.warn(failed);
  }
}

/* Интеграции (разработчик и администратор): API-ключи, вебхуки с журналом доставок и тестовым приёмником,
   журнал событий камер, песочница и документация API. Секреты показываются один раз. */
export function IntegrationsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = TABS.includes(raw as Tab) ? (raw as Tab) : "keys";
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-extrabold">{t("nav.integrations")}</h1>
        <div className="w-full overflow-x-auto sm:w-auto">
          <Tabs items={TABS.map((id) => ({ id, label: t(`integrations.tab.${id}`) }))} value={tab} onChange={(id) => setParams(id === "keys" ? {} : { tab: id })} />
        </div>
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "keys" && <KeysTab />}
        {tab === "webhooks" && <WebhooksTab />}
        {tab === "journal" && <JournalTab />}
        {tab === "sandbox" && <SandboxTab />}
        {tab === "docs" && <DocsTab />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- API-ключи
function KeysTab() {
  const { t, i18n } = useTranslation();
  const keys = useApiKeys();
  const [adding, setAdding] = useState(false);
  const [issued, setIssued] = useState<{ secret: string; key: ApiKeyOut } | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyOut | null>(null);
  const revoke = useRevokeApiKey();
  return (
    <Card>
      <CardTitle right={<Button onClick={() => setAdding(true)} data-testid="add-api-key"><Plus className="h-4 w-4" aria-hidden="true" /> {t("integrations.keys.add")}</Button>}>
        <KeyRound className="mr-2 inline h-5 w-5" aria-hidden="true" /> {t("integrations.keys.title")}
      </CardTitle>
      <p className="mb-3 text-[13px] font-semibold text-ink-2">{t("integrations.keys.hint")}</p>
      {keys.error ? <ErrorBox error={keys.error} onRetry={() => void keys.refetch()} /> : null}
      {keys.isLoading ? (
        <SkeletonRows rows={3} />
      ) : (keys.data ?? []).length === 0 ? (
        <EmptyState title={t("integrations.keys.empty")} hint={t("integrations.keys.emptyHint")} testId="api-keys-empty" />
      ) : (
        <div className="overflow-x-auto">
          <Table data-testid="api-keys">
            <thead>
              <tr>
                <Th>{t("integrations.keys.name")}</Th>
                <Th>{t("integrations.keys.prefix")}</Th>
                <Th>{t("integrations.keys.scopes")}</Th>
                <Th>{t("integrations.keys.expires")}</Th>
                <Th>{t("integrations.keys.lastUsed")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {(keys.data ?? []).map((k) => (
                <tr key={k.id} data-testid="api-key-row">
                  <Td className="font-bold">{k.name}</Td>
                  <Td mono>{k.prefix}…</Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {k.scopes.map((s) => (
                        <Badge key={s} tone="blue">{s}</Badge>
                      ))}
                    </div>
                  </Td>
                  <Td>{k.expires_at ? formatTime(k.expires_at, undefined, i18n.language) : t("integrations.keys.never")}</Td>
                  <Td>{k.last_used_at ? formatTime(k.last_used_at, undefined, i18n.language) : "—"}</Td>
                  <Td className="text-right">
                    {k.active ? (
                      <Button size="sm" variant="ghost" onClick={() => setRevoking(k)} data-testid="revoke-api-key">{t("integrations.keys.revoke")}</Button>
                    ) : (
                      <Badge tone="gray">{k.revoked_at ? t("integrations.keys.revoked") : t("integrations.keys.expired")}</Badge>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
      <Drawer open={adding} onClose={() => setAdding(false)} title={t("integrations.keys.new")}>
        <CreateKeyForm
          onDone={(secret, key) => {
            setAdding(false);
            setIssued({ secret, key });
          }}
        />
      </Drawer>
      <Dialog open={issued !== null} onClose={() => setIssued(null)} title={t("integrations.keys.issuedTitle")}>
        {issued && <SecretView secret={issued.secret} hint={t("integrations.keys.issuedHint")} example={`curl -H "Authorization: Bearer ${issued.secret}" ${window.location.origin}/api/v1/lots`} onClose={() => setIssued(null)} />}
      </Dialog>
      <ReasonDialog
        open={revoking !== null}
        title={fmt(t("integrations.keys.revokeTitle"), { name: revoking?.name ?? "" })}
        confirm={t("integrations.keys.revoke")}
        busy={revoke.isPending}
        onClose={() => setRevoking(null)}
        onConfirm={(reason) =>
          revoking &&
          revoke.mutate(
            { keyId: revoking.id, reason },
            { onSuccess: () => { toast.ok(t("integrations.keys.revokedToast")); setRevoking(null); }, onError: () => toast.bad(t("integrations.failed")) },
          )
        }
      />
    </Card>
  );
}

function CreateKeyForm({ onDone }: { onDone: (secret: string, key: ApiKeyOut) => void }) {
  const { t } = useTranslation();
  const catalog = useIntegrationsCatalog();
  const create = useCreateApiKey();
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["sessions:read"]);
  const [expires, setExpires] = useState("365");
  const grantable = catalog.data?.grantable ?? [];
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    create.mutate(
      { name: name.trim(), scopes, lot_scope: [], expires_in_days: expires === "0" ? null : Number(expires), reason: t("integrations.keys.createReason") },
      { onSuccess: (r) => onDone(r.secret, r.key) },
    );
  };
  return (
    <form onSubmit={submit} className="space-y-4" data-testid="api-key-form">
      <div>
        <Label htmlFor="key-name">{t("integrations.keys.name")}</Label>
        <Input id="key-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("integrations.keys.namePh")} autoFocus />
      </div>
      <fieldset>
        <legend className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("integrations.keys.scopes")}</legend>
        <div className="grid gap-1.5">
          {grantable.map((s) => (
            <label key={s} className="flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-[13.5px] font-semibold">
              <input type="checkbox" checked={scopes.includes(s)} onChange={(e) => setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))} data-scope={s} />
              <span className="font-mono text-[12.5px]">{s}</span>
              <span className="text-ink-2">— {t(`integrations.scope.${s.replace(":", "_")}`, { defaultValue: "" })}</span>
            </label>
          ))}
        </div>
        <p className="mt-1 text-[12px] font-semibold text-muted">{t("integrations.keys.scopesHint")}</p>
      </fieldset>
      <div>
        <Label htmlFor="key-expires">{t("integrations.keys.expires")}</Label>
        <Select id="key-expires" value={expires} onChange={(e) => setExpires(e.target.value)}>
          <option value="30">{fmt(t("integrations.keys.days"), { n: 30 })}</option>
          <option value="90">{fmt(t("integrations.keys.days"), { n: 90 })}</option>
          <option value="365">{fmt(t("integrations.keys.days"), { n: 365 })}</option>
          <option value="0">{t("integrations.keys.never")}</option>
        </Select>
      </div>
      {create.error ? <ErrorBox error={create.error} /> : null}
      <Button type="submit" className="w-full" disabled={create.isPending || name.trim().length < 2 || scopes.length === 0} data-testid="api-key-submit">
        {t("integrations.keys.create")}
      </Button>
    </form>
  );
}

function SecretView({ secret, hint, example, onClose }: { secret: string; hint: string; example?: string; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3" data-testid="issued-secret">
      <p className="text-[14px] font-semibold text-ink-2">{hint}</p>
      <div className="rounded-xl bg-ink px-4 py-3 font-mono text-[13.5px] font-bold break-all text-[#c9f5da]" data-testid="secret-value">{secret}</div>
      {example && <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px] font-semibold text-ink-2">{example}</pre>}
      <div className="flex gap-2">
        <Button variant="secondary" onClick={() => void copyText(secret, t("devices.copied"), t("devices.copyFailed"))}>
          <Copy className="h-4 w-4" aria-hidden="true" /> {t("integrations.copy")}
        </Button>
        <Button className="ml-auto" onClick={onClose}>{t("integrations.savedIt")}</Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- вебхуки
function readInbox(): string | null {
  try {
    return localStorage.getItem(INBOX_KEY);
  } catch {
    return null;
  }
}

function WebhooksTab() {
  const { t } = useTranslation();
  const hooks = useWebhooks();
  const [adding, setAdding] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<WebhookOut | null>(null);
  const [inbox, setInbox] = useState<string | null>(readInbox);
  const del = useDeleteWebhook();
  const saveInbox = (token: string | null) => {
    setInbox(token);
    try {
      if (token) localStorage.setItem(INBOX_KEY, token);
      else localStorage.removeItem(INBOX_KEY);
    } catch {
      /* приватный режим — приёмник живёт до перезагрузки */
    }
  };
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
      <div className="space-y-4">
        <Card>
          <CardTitle right={<Button onClick={() => setAdding(true)} data-testid="add-webhook"><Plus className="h-4 w-4" aria-hidden="true" /> {t("integrations.hooks.add")}</Button>}>
            <Webhook className="mr-2 inline h-5 w-5" aria-hidden="true" /> {t("integrations.hooks.title")}
          </CardTitle>
          <p className="mb-3 text-[13px] font-semibold text-ink-2">{t("integrations.hooks.hint")}</p>
          {hooks.error ? <ErrorBox error={hooks.error} onRetry={() => void hooks.refetch()} /> : null}
          {hooks.isLoading ? (
            <SkeletonRows rows={2} />
          ) : (hooks.data ?? []).length === 0 ? (
            <EmptyState title={t("integrations.hooks.empty")} hint={t("integrations.hooks.emptyHint")} testId="webhooks-empty" />
          ) : (
            <div className="space-y-3" data-testid="webhooks">
              {(hooks.data ?? []).map((h) => (
                <WebhookCard key={h.id} hook={h} onSecret={setSecret} onDelete={() => setDeleting(h)} />
              ))}
            </div>
          )}
        </Card>
        <DeliveriesCard />
      </div>
      <InboxCard token={inbox} onToken={saveInbox} />
      <Drawer open={adding} onClose={() => setAdding(false)} title={t("integrations.hooks.new")}>
        <CreateWebhookForm
          inbox={inbox}
          onInbox={saveInbox}
          onDone={(s) => {
            setAdding(false);
            setSecret(s);
          }}
        />
      </Drawer>
      <Dialog open={secret !== null} onClose={() => setSecret(null)} title={t("integrations.hooks.secretTitle")}>
        {secret && <SecretView secret={secret} hint={t("integrations.hooks.secretHint")} onClose={() => setSecret(null)} />}
      </Dialog>
      <ReasonDialog
        open={deleting !== null}
        title={fmt(t("integrations.hooks.deleteTitle"), { url: deleting?.url ?? "" })}
        confirm={t("integrations.hooks.delete")}
        busy={del.isPending}
        onClose={() => setDeleting(null)}
        onConfirm={(reason) => deleting && del.mutate({ id: deleting.id, reason }, { onSuccess: () => setDeleting(null), onError: () => toast.bad(t("integrations.failed")) })}
      />
    </div>
  );
}

function WebhookCard({ hook, onSecret, onDelete }: { hook: WebhookOut; onSecret: (s: string) => void; onDelete: () => void }) {
  const { t, i18n } = useTranslation();
  const test = useTestWebhook();
  const update = useUpdateWebhook();
  const rotate = useRotateWebhookSecret();
  const failing = hook.consecutive_failures > 0;
  return (
    <div className="rounded-xl border border-line p-3" data-testid="webhook-card">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[13px] font-bold" title={hook.url}>{hook.url}</span>
        <Badge tone={!hook.active ? "gray" : failing ? "yellow" : "green"}>
          {!hook.active ? (hook.disabled_reason === "AUTO_FAILING_3D" ? t("integrations.hooks.autoOff") : t("integrations.hooks.off")) : failing ? fmt(t("integrations.hooks.failing"), { n: hook.consecutive_failures }) : t("integrations.hooks.on")}
        </Badge>
      </div>
      {hook.description && <div className="mt-1 text-[13px] font-semibold text-ink-2">{hook.description}</div>}
      <div className="mt-2 flex flex-wrap gap-1">
        {hook.events.map((e) => (
          <Badge key={e} tone="blue">{e}</Badge>
        ))}
      </div>
      <div className="mt-2 text-[12.5px] font-semibold text-muted">
        {fmt(t("integrations.hooks.stats"), { ok: hook.delivered_24h, failed: hook.failed_24h, dead: hook.dead })}
        {hook.last_delivery_at && ` · ${t("integrations.hooks.last")} ${formatTime(hook.last_delivery_at, undefined, i18n.language)}`}
        {` · ${t("integrations.hooks.secret")} ${hook.secret_hint}`}
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={test.isPending || !hook.active}
          data-testid="webhook-test"
          onClick={() =>
            test.mutate(hook.id, {
              onSuccess: (d) =>
                d.status === "DELIVERED"
                  ? toast.ok(fmt(t("integrations.hooks.testOk"), { code: d.last_status ?? 0, ms: d.last_latency_ms ?? 0 }))
                  : toast.warn(fmt(t("integrations.hooks.testFail"), { error: d.last_error ?? String(d.last_status ?? "") })),
              onError: () => toast.bad(t("integrations.failed")),
            })
          }
        >
          <Send className="h-4 w-4" aria-hidden="true" /> {t("integrations.hooks.test")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => update.mutate({ id: hook.id, body: { active: !hook.active, reason: t("integrations.hooks.toggleReason") } })}
        >
          {hook.active ? t("integrations.hooks.disable") : t("integrations.hooks.enable")}
        </Button>
        <Button size="sm" variant="ghost" disabled={rotate.isPending} onClick={() => rotate.mutate({ id: hook.id, reason: t("integrations.hooks.rotateReason") }, { onSuccess: (r) => onSecret(r.secret) })}>
          <RefreshCw className="h-4 w-4" aria-hidden="true" /> {t("integrations.hooks.rotate")}
        </Button>
        <Button size="sm" variant="ghost" className="ml-auto text-bad" onClick={onDelete} aria-label={t("integrations.hooks.delete")}>
          <Trash2 className="h-4 w-4" aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}

function CreateWebhookForm({ inbox, onInbox, onDone }: { inbox: string | null; onInbox: (t: string) => void; onDone: (secret: string) => void }) {
  const { t } = useTranslation();
  const catalog = useIntegrationsCatalog();
  const create = useCreateWebhook();
  const makeInbox = useCreateInbox();
  const inboxData = useInbox(inbox);
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>(["session.closed"]);
  const [description, setDescription] = useState("");
  const useTestInbox = () => {
    if (inboxData.data) {
      setUrl(inboxData.data.url);
      return;
    }
    makeInbox.mutate(undefined, {
      onSuccess: (r) => {
        onInbox(r.token);
        setUrl(r.url);
      },
    });
  };
  return (
    <form
      className="space-y-4"
      data-testid="webhook-form"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate({ url: url.trim(), events, description: description.trim() || null, reason: t("integrations.hooks.createReason") }, { onSuccess: (r) => onDone(r.secret) });
      }}
    >
      <div>
        <Label htmlFor="hook-url">{t("integrations.hooks.url")}</Label>
        <Input id="hook-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://erp.example.uz/parking/hooks" autoFocus />
        <button type="button" className="mt-1 text-[12.5px] font-bold text-accent" onClick={useTestInbox} data-testid="use-test-inbox">
          {t("integrations.hooks.useInbox")}
        </button>
      </div>
      <fieldset>
        <legend className="mb-1 text-[11px] font-bold uppercase tracking-[0.06em] text-muted">{t("integrations.hooks.events")}</legend>
        <div className="grid gap-1.5">
          {(catalog.data?.events ?? []).map((ev) => (
            <label key={ev} className="flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-[13.5px] font-semibold">
              <input type="checkbox" checked={events.includes(ev)} onChange={(e) => setEvents(e.target.checked ? [...events, ev] : events.filter((x) => x !== ev))} data-event={ev} />
              <span className="font-mono text-[12.5px]">{ev}</span>
              <span className="text-ink-2">— {t(`integrations.event.${ev.replace(".", "_")}`, { defaultValue: "" })}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <div>
        <Label htmlFor="hook-desc">{t("integrations.hooks.description")}</Label>
        <Input id="hook-desc" value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t("integrations.hooks.descriptionPh")} />
      </div>
      <div className="rounded-xl bg-surface-2 p-3 text-[12.5px] font-semibold text-ink-2">{t("integrations.hooks.retryHint")}</div>
      {create.error ? <ErrorBox error={create.error} /> : null}
      <Button type="submit" className="w-full" disabled={create.isPending || url.trim().length < 8 || events.length === 0} data-testid="webhook-submit">
        {t("integrations.hooks.create")}
      </Button>
    </form>
  );
}

type DeliveryFilter = "all" | "DELIVERED" | "RETRY" | "DEAD";

function DeliveriesCard() {
  const { t, i18n } = useTranslation();
  const [filter, setFilter] = useState<DeliveryFilter>("all");
  const deliveries = useDeliveries(null, filter === "all" ? null : filter);
  const redeliver = useRedeliver();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Card>
      <CardTitle right={<Segmented size="sm" ariaLabel={t("integrations.deliveries.filter")} value={filter} onChange={setFilter} options={(["all", "DELIVERED", "RETRY", "DEAD"] as const).map((v) => ({ value: v, label: t(`integrations.deliveries.${v}`) }))} />}>
        {t("integrations.deliveries.title")}
      </CardTitle>
      {deliveries.error ? <ErrorBox error={deliveries.error} /> : null}
      {(deliveries.data ?? []).length === 0 ? (
        <EmptyState title={t("integrations.deliveries.empty")} hint={t("integrations.deliveries.emptyHint")} />
      ) : (
        <div className="overflow-x-auto">
          <Table data-testid="deliveries">
            <thead>
              <tr>
                <Th>{t("integrations.time")}</Th>
                <Th>{t("integrations.deliveries.event")}</Th>
                <Th>{t("integrations.deliveries.status")}</Th>
                <Th>{t("integrations.deliveries.attempts")}</Th>
                <Th>{t("integrations.deliveries.response")}</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {(deliveries.data ?? []).map((d) => (
                <Fragment key={d.id}>
                  <tr data-testid="delivery-row" data-status={d.status} className="cursor-pointer" onClick={() => setOpen(open === d.id ? null : d.id)}>
                    <Td>{formatTime(d.created_at, undefined, i18n.language)}</Td>
                    <Td mono>{d.event}</Td>
                    <Td>
                      <Badge tone={d.status === "DELIVERED" ? "green" : d.status === "DEAD" ? "red" : d.status === "RETRY" ? "yellow" : "gray"}>{t(`integrations.deliveries.${d.status}`)}</Badge>
                    </Td>
                    <Td>{d.attempts}</Td>
                    <Td className="max-w-[260px] truncate" title={d.last_error ?? undefined}>
                      {d.last_status ?? "—"} {d.last_latency_ms !== null && `· ${d.last_latency_ms} мс`} {d.last_error && `· ${d.last_error}`}
                    </Td>
                    <Td className="text-right">
                      {(d.status === "DEAD" || d.status === "RETRY") && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={redeliver.isPending}
                          onClick={(e) => {
                            e.stopPropagation();
                            redeliver.mutate({ id: d.id, reason: t("integrations.deliveries.redeliverReason") }, { onSuccess: () => toast.ok(t("integrations.deliveries.queued")) });
                          }}
                        >
                          {t("integrations.deliveries.redeliver")}
                        </Button>
                      )}
                    </Td>
                  </tr>
                  {open === d.id && (
                    <tr>
                      <Td colSpan={6}>
                        <pre className="max-h-64 overflow-auto rounded-lg bg-surface-2 p-3 text-[12px] font-semibold">{JSON.stringify(d.payload, null, 2)}</pre>
                      </Td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        </div>
      )}
    </Card>
  );
}

function InboxCard({ token, onToken }: { token: string | null; onToken: (t: string | null) => void }) {
  const { t, i18n } = useTranslation();
  const inbox = useInbox(token);
  const create = useCreateInbox();
  const expired = inbox.isError;
  return (
    <Card className="self-start" data-testid="test-inbox">
      <CardTitle>
        <Inbox className="mr-2 inline h-5 w-5" aria-hidden="true" /> {t("integrations.inbox.title")}
      </CardTitle>
      <p className="mb-3 text-[13px] font-semibold text-ink-2">{t("integrations.inbox.hint")}</p>
      {!token || expired ? (
        <Button variant="secondary" disabled={create.isPending} onClick={() => create.mutate(undefined, { onSuccess: (r) => onToken(r.token) })} data-testid="create-inbox">
          {expired ? t("integrations.inbox.recreate") : t("integrations.inbox.create")}
        </Button>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-lg bg-surface-2 px-2 py-1.5 text-[12px] font-bold" data-testid="inbox-url">{inbox.data?.url ?? "…"}</code>
            <Button size="icon" variant="ghost" aria-label={t("integrations.copy")} onClick={() => inbox.data && void copyText(inbox.data.url, t("devices.copied"), t("devices.copyFailed"))}>
              <Copy className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
          {(inbox.data?.items ?? []).length === 0 ? (
            <div className="rounded-xl bg-surface-2 p-3 text-[13px] font-semibold text-muted">{t("integrations.inbox.waiting")}</div>
          ) : (
            <ul className="space-y-2" data-testid="inbox-items">
              {(inbox.data?.items ?? []).map((item, i) => (
                <li key={`${item.received_at}-${i}`} className="rounded-xl border border-line p-2" data-testid="inbox-item" data-event={item.event ?? ""}>
                  <div className="flex items-center gap-2 text-[12.5px] font-bold">
                    <span className="font-mono">{item.event ?? "—"}</span>
                    <span className="text-muted">{formatTime(item.received_at, undefined, i18n.language)}</span>
                    <span className="ml-auto">
                      {item.signature_valid === true && <Badge tone="green">{t("integrations.inbox.signed")}</Badge>}
                      {item.signature_valid === false && <Badge tone="red">{t("integrations.inbox.badSignature")}</Badge>}
                      {item.signature_valid === null && <Badge tone="gray">{t("integrations.inbox.unsigned")}</Badge>}
                    </span>
                  </div>
                  <pre className={cn("mt-1 max-h-40 overflow-auto rounded-lg bg-surface-2 p-2 text-[11.5px] font-semibold")}>{JSON.stringify(item.body, null, 2)}</pre>
                </li>
              ))}
            </ul>
          )}
          <button type="button" className="text-[12px] font-bold text-muted" onClick={() => onToken(null)}>{t("integrations.inbox.forget")}</button>
        </div>
      )}
    </Card>
  );
}
