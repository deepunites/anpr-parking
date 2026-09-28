/** Ошибка API в едином формате бэкенда: `{error: {code, message, correlation_id, details}}`. */
export interface ApiErrorBody {
  error?: { code?: string; message?: string; correlation_id?: string; details?: Record<string, unknown> | null };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly details: Record<string, unknown> | null;

  constructor(status: number, body: ApiErrorBody | null, fallback = "REQUEST_FAILED") {
    const err = body?.error;
    super(err?.message ?? `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = err?.code ?? (status === 0 ? "NETWORK" : fallback);
    this.correlationId = err?.correlation_id ?? null;
    this.details = err?.details ?? null;
  }
}

export function toApiError(status: number, body: unknown): ApiError {
  const parsed = typeof body === "object" && body !== null ? (body as ApiErrorBody) : null;
  return new ApiError(status, parsed);
}
