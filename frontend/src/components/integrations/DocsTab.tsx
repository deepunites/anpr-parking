import { useMemo } from "react";
import { BookOpen } from "lucide-react";
import { useTranslation } from "@/i18n";
import { useApiReference, useIntegrationsCatalog } from "@/api/hooks";
import { Card, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SkeletonRows } from "@/components/ui/skeleton";
import { ErrorBox } from "@/components/ErrorBox";

const METHOD_TONE = { get: "blue", post: "green", put: "yellow", patch: "yellow", delete: "red" } as const;

const VERIFY_PY = `import hashlib, hmac, time

def verify(secret: str, headers: dict, body: bytes) -> bool:
    ts = headers["X-Parking-Timestamp"]
    if abs(time.time() - int(ts)) > 300:  # защита от повторов
        return False
    signed = ts.encode() + b"." + body
    expected = "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-Parking-Signature"])`;

const VERIFY_JS = `import crypto from "node:crypto";

export function verify(secret, headers, rawBody) {
  const ts = headers["x-parking-timestamp"];
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(\`\${ts}.\${rawBody}\`).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(headers["x-parking-signature"]));
}`;

/* Документация для интегратора: как авторизоваться, какие методы доступны ключам и камерам, как проверить
   подпись вебхука. Список методов — из живого OpenAPI сервера (не расходится с кодом). */
export function DocsTab() {
  const { t } = useTranslation();
  const ref = useApiReference(true);
  const catalog = useIntegrationsCatalog();
  const origin = window.location.origin;
  const ops = useMemo(() => {
    const out: { method: string; path: string; summary: string; scope: string }[] = [];
    for (const [path, methods] of Object.entries(ref.data?.paths ?? {})) {
      for (const [method, op] of Object.entries(methods)) {
        out.push({ method, path, summary: op.summary ?? "", scope: op["x-camera-auth"] ? "X-Camera-Key" : (op["x-permission"] ?? []).join(", ") });
      }
    }
    return out.sort((a, b) => a.path.localeCompare(b.path));
  }, [ref.data]);
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card>
        <CardTitle>
          <BookOpen className="mr-2 inline h-5 w-5" aria-hidden="true" /> {t("integrations.docs.auth")}
        </CardTitle>
        <div className="space-y-3 text-[13.5px] font-semibold text-ink-2">
          <p>{t("integrations.docs.authKey")}</p>
          <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px]">{`curl -H "Authorization: Bearer pk_…" ${origin}/api/v1/lots/{lot_id}/sessions`}</pre>
          <p>{t("integrations.docs.authCamera")}</p>
          <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px]">{`curl -H "X-Camera-Key: …" -H "Content-Type: application/json" \\\n  -d @event.json ${origin}/api/v1/camera/events`}</pre>
          <p>{t("integrations.docs.errors")}</p>
          <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px]">{`{"error": {"code": "LOW_CONFIDENCE", "message": "…", "correlation_id": "…"}}`}</pre>
        </div>
      </Card>
      <Card>
        <CardTitle>{t("integrations.docs.webhooks")}</CardTitle>
        <div className="space-y-3 text-[13.5px] font-semibold text-ink-2">
          <p>{t("integrations.docs.webhooksHint")}</p>
          <div className="flex flex-wrap gap-1">
            {(catalog.data?.events ?? []).map((e) => (
              <Badge key={e} tone="blue">{e}</Badge>
            ))}
          </div>
          <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px]">{`POST <ваш URL>
X-Parking-Event: session.closed
X-Parking-Delivery: <id доставки — для идемпотентности>
X-Parking-Timestamp: 1759000000
X-Parking-Signature: sha256=<HMAC-SHA256(secret, timestamp + "." + body)>

{"id": "…", "event": "session.closed", "event_id": "…", "occurred_at": "…",
 "tenant_id": "…", "data": {"session_id": "…", "lot_id": "…", "exit_time": "…", "billing_minutes": 95}}`}</pre>
          <p>{t("integrations.docs.verify")}</p>
          <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px]" data-testid="verify-python">{VERIFY_PY}</pre>
          <pre className="overflow-x-auto rounded-xl bg-surface-2 p-3 text-[12px]">{VERIFY_JS}</pre>
        </div>
      </Card>
      <Card className="xl:col-span-2">
        <CardTitle>{t("integrations.docs.methods")}</CardTitle>
        <p className="mb-3 text-[13px] font-semibold text-ink-2">{t("integrations.docs.methodsHint")}</p>
        {ref.error ? <ErrorBox error={ref.error} /> : null}
        {ref.isLoading ? (
          <SkeletonRows rows={5} />
        ) : (
          <ul className="divide-y divide-line" data-testid="api-methods">
            {ops.map((o) => (
              <li key={`${o.method} ${o.path}`} className="flex flex-wrap items-center gap-2 py-2">
                <Badge tone={METHOD_TONE[o.method as keyof typeof METHOD_TONE] ?? "gray"} className="w-16 justify-center uppercase">{o.method}</Badge>
                <code className="font-mono text-[12.5px] font-bold">{o.path}</code>
                <span className="text-[13px] font-semibold text-ink-2">{o.summary}</span>
                <span className="ml-auto font-mono text-[11.5px] font-bold text-muted">{o.scope}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
