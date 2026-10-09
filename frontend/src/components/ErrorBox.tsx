import { useState } from "react";
import { Link } from "react-router-dom";
import { fmt, useTranslation } from "@/i18n";
import { ApiError } from "@/api/errors";
import { Button } from "@/components/ui/button";
import { copyToClipboard } from "@/lib/clipboard";

/** Ошибка API: человеческий текст по error.code + correlation_id с копированием одной кнопкой (§6). */
export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  if (!error) return null;
  const apiError = error instanceof ApiError ? error : null;
  const code = apiError?.code ?? "REQUEST_FAILED";
  const human = t(`error.codes.${code}`, { defaultValue: apiError?.message ?? String(error) });
  const correlation = apiError?.correlationId;
  // v2 этап 3: лимит тарифа — человеческая строка с цифрами и путь к смене тарифа вместо JSON деталей
  const planIssue = code === "PLAN_LIMIT_REACHED" || code === "PLAN_TOO_SMALL" || code === "SUBSCRIPTION_INACTIVE";
  const d = apiError?.details ?? {};
  const limitLine =
    (code === "PLAN_LIMIT_REACHED" || code === "PLAN_TOO_SMALL") && typeof d.resource === "string"
      ? fmt(t("error.limit"), { resource: t(`sub.${d.resource}`), requested: String(d.requested ?? d.used ?? ""), plan: String(d.plan ?? ""), limit: String(d.limit ?? "") })
      : null;
  const copy = async () => {
    if (!correlation) return;
    try {
      await copyToClipboard(correlation);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard недоступен — id виден текстом */
    }
  };
  return (
    <div role="alert" className="rounded-xl border border-bad/50 bg-bad-soft p-3 text-bad-ink" data-testid="error-box">
      <div className="font-semibold">
        {t("error.title")}: {human} <span className="font-mono text-sm opacity-80">({code})</span>
      </div>
      {limitLine && <div className="mt-1 text-sm font-semibold" data-testid="limit-line">{limitLine}</div>}
      {!planIssue && apiError?.details && Object.keys(apiError.details).length > 0 && (
        <div className="mt-1 font-mono text-xs opacity-80">{JSON.stringify(apiError.details)}</div>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        {correlation && (
          <>
            <span>
              {t("error.correlation")}: <code className="font-mono">{correlation}</code>
            </span>
            <Button size="sm" variant="outline" onClick={copy} data-testid="copy-correlation">
              {copied ? t("error.copied") : t("error.copy")}
            </Button>
          </>
        )}
        {planIssue && code !== "PLAN_TOO_SMALL" && (
          <Link to="/subscription" className="font-bold underline" data-testid="upgrade-link">
            {t("error.upgrade")}
          </Link>
        )}
        {onRetry && (
          <Button size="sm" variant="secondary" onClick={onRetry}>
            {t("error.retry")}
          </Button>
        )}
      </div>
    </div>
  );
}
