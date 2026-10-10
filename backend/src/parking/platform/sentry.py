"""Sentry (фаза 7 §6): release/environment, теги контекста, `before_send` — последняя линия перед утечкой ПДн:
вырезает номера, JWT, ключи, заголовки авторизации/cookie; ожидаемые доменные ошибки не отправляются."""

from __future__ import annotations

from typing import Any

from parking.platform.logging import redact_text, redact_value
from parking.shared.correlation import correlation_id_var, tenant_id_var, user_id_var
from parking.shared.errors import DomainError

_DROP_HEADERS = {"authorization", "cookie", "set-cookie", "x-camera-key", "x-auth", "auth"}


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """before_send: None — не отправлять (ожидаемая доменная ошибка); иначе событие без ПДн и секретов."""
    exc = (hint or {}).get("exc_info")
    if exc and isinstance(exc[1], DomainError):
        return None  # дубликат, blacklist, отклонённый платёж — это INFO бизнеса, не инцидент
    for key in ("message", "logentry", "extra", "contexts", "breadcrumbs", "exception", "tags", "user"):
        if key in event:
            event[key] = redact_value(event[key], key if key not in {"message", "logentry"} else None)
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                k: ("[REDACTED]" if k.lower() in _DROP_HEADERS else redact_text(str(v))) for k, v in headers.items()
            }
        request.pop("cookies", None)
        request.pop("data", None)  # тела запросов не отправляются вовсе (max_request_body_size="never" + защита)
        if "url" in request:  # ключ камеры в пути (Hikvision/Dahua без своего заголовка) — маской
            request["url"] = redact_text(str(request["url"]))  # и ключ камеры в пути (mask_camera_key)
        if "query_string" in request:
            request["query_string"] = redact_text(str(request["query_string"]))
    tags = dict(event.get("tags") or {})
    cid = correlation_id_var.get()
    if cid:
        tags.setdefault("correlation_id", cid)
    tenant = tenant_id_var.get()
    if tenant:
        tags.setdefault("tenant_id", str(tenant))
    if user_id_var.get():
        tags.setdefault("user_id", str(user_id_var.get()))
    event["tags"] = tags
    return event


def init_sentry(*, dsn: str, environment: str, release: str, traces_sample_rate: float = 0.0) -> bool:
    if not dsn:
        return False
    import sentry_sdk  # noqa: PLC0415 — тяжёлый импорт только при включённом Sentry
    from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: PLC0415
    from sentry_sdk.integrations.starlette import StarletteIntegration  # noqa: PLC0415

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        before_send=scrub_event,  # type: ignore[arg-type]
        before_send_transaction=scrub_event,  # type: ignore[arg-type]  # трассы тоже: request.url с ключом камеры
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )
    return True
