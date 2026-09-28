/**
 * Инициализация Sentry во фронтенде (фаза 7 §6): только при заданном VITE_SENTRY_DSN, SDK грузится отдельным
 * чанком (dynamic import), release = VITE_RELEASE (commit sha из CI; source maps загружаются в CI `sentry-cli
 * sourcemaps upload` — build.sourcemap="hidden", карты не ссылаются из бандла и не раздаются nginx'ом).
 * Теги: tenant_id, lot_id, role, correlation_id (из ApiError). Никаких ПДн — см. scrub.ts.
 */
import { scrubEvent } from "./scrub";

export interface SentryConfig {
  dsn: string;
  release: string;
  environment: string;
}

export function sentryConfigFromEnv(env: Record<string, string | undefined>): SentryConfig | null {
  const dsn = env.VITE_SENTRY_DSN;
  if (!dsn) return null;
  return { dsn, release: env.VITE_RELEASE || "dev", environment: env.VITE_ENVIRONMENT || env.MODE || "production" };
}

export async function initSentry(config: SentryConfig | null): Promise<boolean> {
  if (config === null) return false;
  const Sentry = await import("@sentry/react");
  Sentry.init({
    dsn: config.dsn,
    release: `parking-frontend@${config.release}`,
    environment: config.environment,
    sendDefaultPii: false,
    tracesSampleRate: 0,
    replaysSessionSampleRate: 0,
    beforeSend: (event, hint) => scrubEvent(event, hint),
    beforeBreadcrumb: (breadcrumb) => (breadcrumb.category === "console" ? null : breadcrumb),
  });
  return true;
}

/** Контекст пользователя без ПДн: только идентификаторы и роль. */
export async function setSentryUser(user: { user_id: string; tenant_id: string; role: string } | null): Promise<void> {
  const Sentry = await import("@sentry/react");
  Sentry.setUser(user ? { id: user.user_id } : null);
  Sentry.setTags({ tenant_id: user?.tenant_id ?? null, role: user?.role ?? null });
}

export async function setSentryLot(lotId: string | null): Promise<void> {
  const Sentry = await import("@sentry/react");
  Sentry.setTag("lot_id", lotId ?? null);
}
