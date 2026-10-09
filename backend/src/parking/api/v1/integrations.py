"""v2 этап 6 «Интеграции» (роль «разработчик»): API-ключи, исходящие вебхуки и журнал доставок, журнал событий
камер, песочница (проверка события без записи), тестовый приёмник вебхуков, документация API для ключей."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Path, Query, Request, status
from pydantic import BaseModel, Field

from parking.api.deps import PUBLIC, get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.api.v1.camera import _parse_xml, dahua_input, hikvision_input
from parking.modules.anpr.application.ingest import CameraEventInput
from parking.modules.devices.application.contracts import CameraPrincipal
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.integrations.application.contracts import (
    EVENT_CATALOG,
    GRANTABLE_SCOPES,
    JournalEntry,
    Outcome,
    mask_plate,
)
from parking.runtime.container import Container
from parking.shared.errors import DomainError, NotFoundError, RateLimitedError, ValidationError

router = APIRouter(prefix="/api/v1", tags=["integrations"])
MANAGE = Permission.INTEGRATIONS_MANAGE
INBOX_TTL = 24 * 3600
INBOX_KEEP = 20
INBOX_MAX_BODY = 64 * 1024
INBOX_CREATE_PER_HOUR = 10  # v2 этап 8: приёмник живёт в Redis — клиент не может завалить память созданием


# ---------------------------------------------------------------- API-ключи
class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    lot_scope: list[str]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    active: bool


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    scopes: list[str] = Field(min_length=1, max_length=len(GRANTABLE_SCOPES))
    lot_scope: list[UUID] = Field(default_factory=list)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    reason: Reason


class IssuedApiKeyOut(BaseModel):
    key: ApiKeyOut
    secret: str = Field(description="полный ключ — показывается один раз; передавать как Authorization: Bearer")


class ScopesOut(BaseModel):
    scopes: list[str]
    grantable: list[str] = Field(description="что может выдать текущий пользователь")
    events: list[str]


class ReasonBody(BaseModel):
    reason: Reason


def _key_out(k: Any, now: datetime) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(k.id),
        name=k.name,
        prefix=k.prefix,
        scopes=list(k.scopes),
        lot_scope=sorted(str(lot) for lot in k.lot_scope),
        created_at=k.created_at,
        expires_at=k.expires_at,
        last_used_at=k.last_used_at,
        revoked_at=k.revoked_at,
        active=k.usable(now),
    )


@router.get("/integrations/catalog", response_model=ScopesOut, openapi_extra=permission_extra(MANAGE))
async def catalog(user: Annotated[CurrentUser, Depends(require(MANAGE))]) -> ScopesOut:
    own = {p.value for p in user.permissions}
    return ScopesOut(
        scopes=list(GRANTABLE_SCOPES), grantable=[s for s in GRANTABLE_SCOPES if s in own], events=list(EVENT_CATALOG)
    )


@router.get("/api-keys", response_model=list[ApiKeyOut], openapi_extra=permission_extra(MANAGE))
async def list_keys(
    user: Annotated[CurrentUser, Depends(require(MANAGE))], container: Annotated[Container, Depends(get_container)]
) -> list[ApiKeyOut]:
    now = container.clock.now()
    return [_key_out(k, now) for k in await container.api_keys.keys(user)]


@router.post(
    "/api-keys",
    response_model=IssuedApiKeyOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(MANAGE),
)
async def create_key(
    body: CreateApiKeyRequest,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> IssuedApiKeyOut:
    issued = await container.api_keys.create(
        user,
        name=body.name,
        scopes=body.scopes,
        lot_scope=frozenset(body.lot_scope),
        expires_in_days=body.expires_in_days,
        reason=body.reason,
    )
    return IssuedApiKeyOut(key=_key_out(issued.key, container.clock.now()), secret=issued.raw)


@router.post(
    "/api-keys/{api_key_id}/revoke", status_code=status.HTTP_204_NO_CONTENT, openapi_extra=permission_extra(MANAGE)
)
async def revoke_key(
    api_key_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.api_keys.revoke(user, api_key_id, reason=body.reason)


# ---------------------------------------------------------------- вебхуки
class WebhookOut(BaseModel):
    id: str
    url: str
    description: str | None
    events: list[str]
    secret_hint: str
    active: bool
    consecutive_failures: int
    created_at: datetime
    disabled_reason: str | None = Field(default=None, description="AUTO_FAILING_3D — выключен после 3 суток отказов")
    delivered_24h: int = 0
    failed_24h: int = 0
    dead: int = 0
    last_delivery_at: datetime | None = None


class CreateWebhookRequest(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    events: list[str] = Field(min_length=1, max_length=len(EVENT_CATALOG))
    description: str | None = Field(default=None, max_length=200)
    reason: Reason


class UpdateWebhookRequest(BaseModel):
    url: str | None = Field(default=None, min_length=8, max_length=500)
    events: list[str] | None = Field(default=None, min_length=1, max_length=len(EVENT_CATALOG))
    description: str | None = Field(default=None, max_length=200)
    active: bool | None = None
    reason: Reason


class IssuedWebhookOut(BaseModel):
    webhook: WebhookOut
    secret: str = Field(description="секрет подписи — показывается один раз")


class DeliveryOut(BaseModel):
    id: str
    endpoint_id: str
    event: str
    event_id: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    last_status: int | None
    last_error: str | None
    last_latency_ms: int | None
    created_at: datetime
    delivered_at: datetime | None
    payload: dict[str, Any]


def _webhook_out(e: Any, stats: dict[str, Any] | None = None) -> WebhookOut:
    st = stats or {}
    return WebhookOut(
        id=str(e.id),
        url=e.url,
        description=e.description,
        events=list(e.events),
        secret_hint=e.secret_hint,
        active=e.active,
        consecutive_failures=e.consecutive_failures,
        created_at=e.created_at,
        disabled_reason=e.disabled_reason,
        delivered_24h=int(st.get("ok_24h", 0)),
        failed_24h=int(st.get("failed_24h", 0)),
        dead=int(st.get("dead", 0)),
        last_delivery_at=st.get("last_at"),
    )


def _delivery_out(d: Any) -> DeliveryOut:
    return DeliveryOut(
        id=str(d.id),
        endpoint_id=str(d.endpoint_id),
        event=d.event_type,
        event_id=str(d.event_id),
        status=d.status,
        attempts=d.attempts,
        next_attempt_at=d.next_attempt_at,
        last_status=d.last_status,
        last_error=d.last_error,
        last_latency_ms=d.last_latency_ms,
        created_at=d.created_at,
        delivered_at=d.delivered_at,
        payload=d.payload,
    )


@router.get("/webhooks", response_model=list[WebhookOut], openapi_extra=permission_extra(MANAGE))
async def list_webhooks(
    user: Annotated[CurrentUser, Depends(require(MANAGE))], container: Annotated[Container, Depends(get_container)]
) -> list[WebhookOut]:
    endpoints, stats = await container.outgoing_webhooks.endpoints(user)
    return [_webhook_out(e, stats.get(e.id)) for e in endpoints]


@router.post(
    "/webhooks",
    response_model=IssuedWebhookOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(MANAGE),
)
async def create_webhook(
    body: CreateWebhookRequest,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> IssuedWebhookOut:
    issued = await container.outgoing_webhooks.create(
        user, url=body.url, events=body.events, description=body.description, reason=body.reason
    )
    return IssuedWebhookOut(webhook=_webhook_out(issued.endpoint), secret=issued.secret)


@router.patch("/webhooks/{endpoint_id}", response_model=WebhookOut, openapi_extra=permission_extra(MANAGE))
async def update_webhook(
    endpoint_id: UUID,
    body: UpdateWebhookRequest,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> WebhookOut:
    endpoint = await container.outgoing_webhooks.update(
        user,
        endpoint_id,
        url=body.url,
        events=body.events,
        description=body.description,
        active=body.active,
        reason=body.reason,
    )
    return _webhook_out(endpoint)


@router.post(
    "/webhooks/{endpoint_id}/delete", status_code=status.HTTP_204_NO_CONTENT, openapi_extra=permission_extra(MANAGE)
)
async def delete_webhook(
    endpoint_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.outgoing_webhooks.delete(user, endpoint_id, reason=body.reason)


class SecretOut(BaseModel):
    secret: str


@router.post("/webhooks/{endpoint_id}/rotate-secret", response_model=SecretOut, openapi_extra=permission_extra(MANAGE))
async def rotate_webhook_secret(
    endpoint_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> SecretOut:
    return SecretOut(secret=await container.outgoing_webhooks.rotate_secret(user, endpoint_id, reason=body.reason))


@router.post("/webhooks/{endpoint_id}/test", response_model=DeliveryOut, openapi_extra=permission_extra(MANAGE))
async def test_webhook(
    endpoint_id: UUID,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> DeliveryOut:
    """Тестовое событие webhook.test сразу — итог (код ответа, задержка, ошибка) в ответе."""
    return _delivery_out(await container.outgoing_webhooks.send_test(user, endpoint_id))


@router.get("/webhook-deliveries", response_model=list[DeliveryOut], openapi_extra=permission_extra(MANAGE))
async def list_deliveries(
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
    endpoint_id: UUID | None = None,
    status_filter: Annotated[Literal["PENDING", "RETRY", "DELIVERED", "DEAD"] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeliveryOut]:
    rows = await container.outgoing_webhooks.deliveries(
        user, endpoint_id=endpoint_id, status=status_filter, limit=limit
    )
    return [_delivery_out(d) for d in rows]


@router.post(
    "/webhook-deliveries/{delivery_id}/redeliver",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(MANAGE),
)
async def redeliver(
    delivery_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.outgoing_webhooks.redeliver(user, delivery_id, reason=body.reason)


# ---------------------------------------------------------------- журнал событий
class JournalOut(BaseModel):
    received_at: datetime
    source: str
    outcome: str
    status: str | None
    http_status: int | None
    event_type: str | None
    plate_masked: str | None
    message: str | None
    correlation_id: str | None
    latency_ms: int | None
    lot_id: str | None
    camera_id: str | None


@router.get("/integrations/journal", response_model=list[JournalOut], openapi_extra=permission_extra(MANAGE))
async def journal(
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
    lot_id: UUID | None = None,
    camera_id: UUID | None = None,
    outcome: Annotated[Literal["ACCEPTED", "DUPLICATE", "QUEUED", "REJECTED", "ERROR"] | None, Query()] = None,
    correlation_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[JournalOut]:
    """Запросы камер за 7 дней: что пришло, что ответили, почему отклонили. Номер — только маской."""
    rows = await container.journal.search(
        user, lot_id=lot_id, outcome=outcome, correlation_id=correlation_id, camera_id=camera_id, limit=limit
    )
    return [
        JournalOut(
            received_at=r.received_at,
            source=r.source,
            outcome=r.outcome,
            status=r.status,
            http_status=r.http_status,
            event_type=r.event_type,
            plate_masked=r.plate_masked,
            message=r.message,
            correlation_id=str(r.correlation_id) if r.correlation_id else None,
            latency_ms=r.latency_ms,
            lot_id=str(r.lot_id) if r.lot_id else None,
            camera_id=str(r.camera_id) if r.camera_id else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------- песочница
class SandboxRequest(BaseModel):
    camera_id: UUID
    vendor: Literal["generic", "hikvision", "dahua"] = "generic"
    payload: str = Field(min_length=2, max_length=16 * 1024, description="тело запроса камеры как есть")


class SandboxOut(BaseModel):
    accepted: bool
    reason: str | None = Field(description="код отказа — тот же, что получила бы камера")
    message: str | None
    event_type: str | None = None
    plate: str | None = None
    plate_format: str | None = None
    confidence: int | None = None
    confidence_class: str | None = None
    event_time: datetime | None = None
    late: bool | None = None
    access_kind: str | None = None
    opens_barrier: bool | None = None


async def _principal(container: Container, user: CurrentUser, camera_id: UUID) -> CameraPrincipal:
    camera = await container.devices.get_camera(user, camera_id)
    lot = await container.lots.get(user, camera.lot_id)
    return CameraPrincipal(
        key_id=uuid4(),
        tenant_id=camera.tenant_id,
        camera_id=camera.id,
        lot_id=camera.lot_id,
        code=camera.code,
        direction=camera.direction.value,
        confidence_min=camera.confidence_min,
        confidence_review_min=camera.confidence_review_min,
        max_clock_skew_seconds=camera.max_clock_skew_seconds,
        late_event_window_hours=lot.policies.late_event_window_hours,
    )


def _sandbox_input(body: SandboxRequest, principal: CameraPrincipal, now: datetime) -> CameraEventInput:
    if body.vendor == "hikvision":
        return hikvision_input(
            _parse_xml(body.payload.encode()),
            code=principal.code,
            event_type=None,
            now=now,
            image_key=None,
            camera_direction=principal.direction,
        )
    try:
        data: dict[str, Any] = json.loads(body.payload)
    except ValueError as exc:
        raise ValidationError("malformed JSON") from exc
    if body.vendor == "dahua":
        return dahua_input(data, code=principal.code, now=now, image_key=None)
    try:
        return CameraEventInput(
            camera_code=str(data["camera_code"]),
            event_type=str(data["event_type"]),
            plate_number=str(data["plate_number"]),
            confidence=int(data["confidence"]),
            event_time=datetime.fromisoformat(str(data["event_time"])),
            image_object_key=None,
            direction=data.get("direction"),
            recognition=data.get("recognition"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValidationError(f"generic event: missing or invalid field {exc}") from exc


@router.post("/integrations/sandbox", response_model=SandboxOut, openapi_extra=permission_extra(MANAGE))
async def sandbox(
    body: SandboxRequest,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> SandboxOut:
    """Проверка события камеры без записи: разбор формата вендора, проверки приёма, решение доступа по номеру.
    Сессия не создаётся, стрела не открывается; попытка видна в журнале событий с источником «sandbox»."""
    principal = await _principal(container, user, body.camera_id)
    now = container.clock.now()
    data: CameraEventInput | None = None
    try:
        data = _sandbox_input(body, principal, now)
        event = container.ingest.check(principal, data)
    except DomainError as exc:
        _sandbox_journal(container, principal, data, Outcome.REJECTED, exc.code, exc.message)
        return SandboxOut(
            accepted=False,
            reason=exc.code,
            message=exc.message,
            event_type=data.event_type if data else None,
            plate=data.plate_number if data else None,
            confidence=data.confidence if data else None,
        )
    decision = await container.access_resolver.resolve(
        user.tenant_id, principal.lot_id, container.plate_crypto.hash(user.tenant_id, event.plate), now
    )
    _sandbox_journal(container, principal, data, Outcome.ACCEPTED, "SANDBOX_OK", None)
    return SandboxOut(
        accepted=True,
        reason=None,
        message=None,
        event_type=event.event_type.value,
        plate=event.plate.value,
        plate_format=event.plate.format.value if hasattr(event.plate, "format") else None,
        confidence=event.confidence,
        confidence_class=event.confidence_class.value,
        event_time=event.event_time,
        late=event.time.late,
        access_kind=decision.kind.value,
        opens_barrier=decision.opens_barrier_automatically,
    )


def _sandbox_journal(
    container: Container,
    principal: CameraPrincipal,
    data: CameraEventInput | None,
    outcome: str,
    status: str,
    message: str | None,
) -> None:
    container.journal.enqueue(
        JournalEntry(
            tenant_id=principal.tenant_id,
            lot_id=principal.lot_id,
            camera_id=principal.camera_id,
            source="sandbox",
            outcome=outcome,
            received_at=container.clock.now(),
            status=status,
            event_type=data.event_type if data else None,
            plate_masked=mask_plate(data.plate_number) if data else None,
            message=message,
        )
    )


# ---------------------------------------------------------------- тестовый приёмник вебхуков
class InboxOut(BaseModel):
    token: str
    url: str
    expires_in: int


class InboxItemOut(BaseModel):
    received_at: str
    event: str | None
    delivery: str | None
    signature: str | None
    signature_valid: bool | None
    body: Any


class InboxContentOut(BaseModel):
    url: str
    items: list[InboxItemOut]


def _inbox_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/hooks/inbox/{token}"


@router.post("/integrations/test-inbox", response_model=InboxOut, openapi_extra=permission_extra(MANAGE))
async def create_inbox(
    request: Request,
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> InboxOut:
    """Тестовый приёмник на 24 часа: адрес для вебхука, пришедшие запросы и проверка их подписи — без своего сервера."""
    quota = f"inbox:create:{user.tenant_id}"
    created = int(await container.redis.incr(quota))
    if created == 1:
        await container.redis.expire(quota, 3600)
    if created > INBOX_CREATE_PER_HOUR:
        raise RateLimitedError("test inbox creation limit", retry_after_seconds=3600)
    token = secrets.token_urlsafe(24)
    await container.redis.set(f"inbox:{token}:tenant", str(user.tenant_id), ex=INBOX_TTL)
    return InboxOut(token=token, url=_inbox_url(request, token), expires_in=INBOX_TTL)


@router.get("/integrations/test-inbox/{token}", response_model=InboxContentOut, openapi_extra=permission_extra(MANAGE))
async def read_inbox(
    request: Request,
    token: Annotated[str, Path(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")],
    user: Annotated[CurrentUser, Depends(require(MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> InboxContentOut:
    owner = await container.redis.get(f"inbox:{token}:tenant")
    if owner is None or (owner.decode() if isinstance(owner, bytes) else str(owner)) != str(user.tenant_id):
        raise NotFoundError("test inbox not found or expired")
    url = _inbox_url(request, token)
    secrets_ = await container.outgoing_webhooks.secrets_for_url(user.tenant_id, url)
    items: list[InboxItemOut] = []
    for raw in await container.redis.lrange(f"inbox:{token}:items", 0, INBOX_KEEP - 1):
        item = json.loads(raw)
        items.append(
            InboxItemOut(
                received_at=item["received_at"],
                event=item.get("event"),
                delivery=item.get("delivery"),
                signature=item.get("signature"),
                signature_valid=_verify(secrets_, item) if secrets_ and item.get("signature") else None,
                body=_as_json(item.get("body", "")),
            )
        )
    return InboxContentOut(url=url, items=items)


def _as_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _verify(candidates: list[bytes], item: dict[str, Any]) -> bool:
    body = str(item.get("body", "")).encode()
    signed = str(item.get("timestamp", "")).encode() + b"." + body
    expected = str(item.get("signature", ""))
    return any(
        hmac.compare_digest(f"sha256={hmac.new(s, signed, hashlib.sha256).hexdigest()}", expected) for s in candidates
    )


@router.post("/hooks/inbox/{token}", status_code=status.HTTP_200_OK, openapi_extra=PUBLIC)
async def inbox_receive(
    request: Request,
    token: Annotated[str, Path(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, str]:
    """Публичный приёмник: хранит последние 20 запросов 24 часа, тело до 64 КБ, не чаще 60 запросов в минуту."""
    if await container.redis.get(f"inbox:{token}:tenant") is None:
        raise NotFoundError("test inbox not found or expired")
    rate_key = f"inbox:{token}:rate"
    hits = int(await container.redis.incr(rate_key))
    if hits == 1:
        await container.redis.expire(rate_key, 60)
    if hits > 60:
        raise RateLimitedError("test inbox rate limit", retry_after_seconds=60)
    body = (await request.body())[:INBOX_MAX_BODY]
    item = {
        "received_at": container.clock.now().isoformat(),
        "event": request.headers.get("x-parking-event"),
        "delivery": request.headers.get("x-parking-delivery"),
        "timestamp": request.headers.get("x-parking-timestamp"),
        "signature": request.headers.get("x-parking-signature"),
        "body": body.decode("utf-8", errors="replace"),
    }
    key = f"inbox:{token}:items"
    await container.redis.lpush(key, json.dumps(item, ensure_ascii=False))
    await container.redis.ltrim(key, 0, INBOX_KEEP - 1)
    await container.redis.expire(key, INBOX_TTL)
    return {"status": "received"}


# ---------------------------------------------------------------- документация API для ключей
@router.get("/integrations/openapi", openapi_extra=permission_extra(MANAGE))
async def api_reference(request: Request, user: Annotated[CurrentUser, Depends(require(MANAGE))]) -> dict[str, Any]:
    """Часть OpenAPI, доступная API-ключам (x-permission ⊆ разрешённых прав ключей) и камерам."""
    spec = request.app.openapi()
    grantable = set(GRANTABLE_SCOPES)
    paths: dict[str, Any] = {}
    for path, ops in spec.get("paths", {}).items():
        kept = {
            m: op
            for m, op in ops.items()
            if isinstance(op, dict)
            and (op.get("x-camera-auth") or (op.get("x-permission") and set(op.get("x-permission", [])) <= grantable))
        }
        if kept:
            paths[path] = kept
    return {
        "openapi": spec.get("openapi"),
        "info": spec.get("info"),
        "paths": paths,
        "components": spec.get("components"),
    }
