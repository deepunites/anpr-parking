/**
 * before_send для Sentry (фаза 7 §6): последняя линия перед утечкой ПДн во внешний сервис.
 * Чистая функция без зависимости от @sentry/react — тестируется vitest'ом. Правила те же, что у backend-редактора:
 * номера (узбекские форматы), JWT, Bearer, ключи камер/длинные секреты, заголовки авторизации, тела запросов.
 * Ожидаемые доменные ошибки API (дубликат, blacklist, платёж отклонён, 4xx валидации) не отправляются.
 */
export const REDACTED = "[REDACTED]";

const JWT = /\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b/g;
const BEARER = /\bbearer\s+[A-Za-z0-9._-]+/gi;
const PLATE = /(?<![A-Z0-9])\d{2}\s?[A-ZА-Я]\s?\d{3}\s?[A-ZА-Я]{2}(?![A-Z0-9])|(?<![A-Z0-9])\d{5}\s?[A-ZА-Я]{3}(?![A-Z0-9])/g;
const LONG_KEY = /\b(?=[A-Za-z0-9_]{40,}\b)(?=[A-Za-z0-9_]*[A-Za-z])(?=[A-Za-z0-9_]*\d)[A-Za-z0-9_]{40,}\b/g;
const SENSITIVE_KEY =
  /(password|passwd|secret|token|jwt|authorization|cookie|api[_-]?key|x-camera-key|pepper|private[_-]?key|plate(?!_hash|_format)|plate_number|raw_plate|(?<![a-z])(card|pan|cvv)(?![a-z]))/i;
const DROP_HEADERS = new Set(["authorization", "cookie", "set-cookie", "x-camera-key", "x-auth", "auth"]);

/** Коды ошибок API, которые являются штатным поведением, а не инцидентом. */
export const EXPECTED_CODES = new Set([
  "DUPLICATE",
  "DUPLICATE_EVENT",
  "BLACKLISTED",
  "BLACKLIST_HIT",
  "PAYMENT_DECLINED",
  "PAYMENT_REJECTED",
  "VALIDATION_ERROR",
  "NOT_FOUND",
  "FORBIDDEN",
  "UNAUTHORIZED",
  "CONFLICT",
  "RATE_LIMITED",
]);

export function redactText(value: string): string {
  return value.replace(JWT, REDACTED).replace(BEARER, `Bearer ${REDACTED}`).replace(PLATE, REDACTED).replace(LONG_KEY, REDACTED);
}

export function redactValue(value: unknown, key?: string): unknown {
  if (key !== undefined && SENSITIVE_KEY.test(key)) return REDACTED;
  if (typeof value === "string") return redactText(value);
  if (Array.isArray(value)) return value.map((v) => redactValue(v));
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) out[k] = redactValue(v, k);
    return out;
  }
  return value;
}

/** Минимальная форма события Sentry, с которой работает скраббер (совместима с ErrorEvent из @sentry/core). */
export interface ScrubbableEvent {
  message?: string;
  logentry?: { message?: string; params?: unknown[] };
  extra?: Record<string, unknown>;
  contexts?: Record<string, unknown>;
  breadcrumbs?: { message?: string; data?: Record<string, unknown>; category?: string }[];
  exception?: { values?: { type?: string; value?: string }[] };
  tags?: Record<string, unknown>;
  user?: Record<string, unknown>;
  request?: {
    url?: string;
    query_string?: unknown;
    headers?: Record<string, string>;
    cookies?: unknown;
    data?: unknown;
  };
}

export interface ScrubHint {
  originalException?: unknown;
}

function isExpected(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const code = (error as { code?: unknown }).code;
  const status = (error as { status?: unknown }).status;
  if (typeof code === "string" && EXPECTED_CODES.has(code)) return true;
  return typeof status === "number" && status >= 400 && status < 500 && status !== 408;
}

/** Возвращает null (не отправлять) для ожидаемых ошибок, иначе событие без ПДн и секретов. */
export function scrubEvent<E extends ScrubbableEvent>(event: E, hint?: ScrubHint): E | null {
  if (hint && isExpected(hint.originalException)) return null;
  if (event.message !== undefined) event.message = redactText(event.message);
  if (event.logentry) event.logentry = redactValue(event.logentry) as E["logentry"];
  if (event.extra) event.extra = redactValue(event.extra) as Record<string, unknown>;
  if (event.contexts) event.contexts = redactValue(event.contexts) as Record<string, unknown>;
  if (event.breadcrumbs) event.breadcrumbs = redactValue(event.breadcrumbs) as E["breadcrumbs"];
  if (event.exception) event.exception = redactValue(event.exception) as E["exception"];
  if (event.tags) event.tags = redactValue(event.tags) as Record<string, unknown>;
  if (event.user) {
    const { id, tenant_id, role } = event.user as Record<string, unknown>;
    event.user = { ...(id !== undefined ? { id } : {}), ...(tenant_id !== undefined ? { tenant_id } : {}), ...(role !== undefined ? { role } : {}) };
  }
  if (event.request) {
    const request = event.request;
    delete request.cookies;
    delete request.data; // тела запросов не отправляются вовсе
    if (request.url) request.url = redactText(request.url);
    if (request.query_string !== undefined) request.query_string = redactText(String(request.query_string));
    if (request.headers) {
      const headers: Record<string, string> = {};
      for (const [k, v] of Object.entries(request.headers)) headers[k] = DROP_HEADERS.has(k.toLowerCase()) ? REDACTED : redactText(v);
      request.headers = headers;
    }
  }
  return event;
}
