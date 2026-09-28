"""Исходящие вебхуки: подписки клиента, постановка доставок по событиям, отправка с подписью, повторы и DLQ.

Постановка — в обработчике стрима (идемпотентно: одна доставка на пару подписка+событие). Отправка — диспетчер
поверх всех клиентов: забирает наступившие доставки с «арендой» (другой диспетчер их не возьмёт), шлёт вне
транзакции (I-9), пишет итог. Неудача → повтор через 1, 5, 30 минут → DEAD; из DEAD — повтор вручную."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.integrations.application.ports import (
    DeliveryQueue,
    SecretBox,
    SendResult,
    WebhookRepository,
    WebhookSender,
)
from parking.modules.integrations.domain.webhook import (
    PUBLIC_NAME,
    TEST_EVENT,
    Delivery,
    DeliveryStatus,
    WebhookEndpoint,
    check_events,
    check_url,
    envelope,
    next_attempt,
    sign,
)
from parking.platform import metrics
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.clock import Clock
from parking.shared.errors import NotFoundError, ValidationError

log = get_logger("integrations.webhooks")
SECRET_PURPOSE = "webhook"  # noqa: S105 — назначение ключа в AAD, не секрет
LEASE = timedelta(minutes=2)
MAX_LIST = 200


@dataclass(frozen=True, slots=True)
class WebhookRepositories:
    webhooks: WebhookRepository
    audit: AuditWriter


@dataclass(frozen=True, slots=True)
class IssuedWebhook:
    endpoint: WebhookEndpoint
    secret: str  # показывается один раз


def _new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class OutgoingWebhookService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        repos: Callable[[UnitOfWork], WebhookRepositories],
        queue: DeliveryQueue,
        box: SecretBox,
        sender: WebhookSender,
        clock: Clock,
        allow_http: bool,
    ) -> None:
        self._uow_factory = uow_factory
        self._repos = repos
        self._queue = queue
        self._box = box
        self._sender = sender
        self._clock = clock
        self._allow_http = allow_http

    # ---------- подписки ----------
    async def create(
        self, actor: CurrentUser, *, url: str, events: list[str], description: str | None, reason: str
    ) -> IssuedWebhook:
        checked_url = check_url(url, allow_http=self._allow_http)
        checked_events = check_events(events)
        secret = _new_secret()
        endpoint = WebhookEndpoint(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            url=checked_url,
            events=checked_events,
            secret_hint=secret[:10] + "…",
            active=True,
            created_by=actor.user_id,
            created_at=self._clock.now(),
            description=(description or "").strip() or None,
        )
        sealed = self._box.seal(actor.tenant_id, f"{SECRET_PURPOSE}:{endpoint.id}", secret.encode())
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            await repos.webhooks.add_endpoint(endpoint, sealed)
            await self._audit(repos, actor, "WEBHOOK_CREATED", endpoint.id, reason, {"events": list(checked_events)})
        return IssuedWebhook(endpoint=endpoint, secret=secret)

    async def endpoints(self, actor: CurrentUser) -> tuple[list[WebhookEndpoint], dict[UUID, dict[str, Any]]]:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            return await repos.webhooks.endpoints(), await repos.webhooks.stats()

    async def update(
        self,
        actor: CurrentUser,
        endpoint_id: UUID,
        *,
        reason: str,
        url: str | None = None,
        events: list[str] | None = None,
        description: str | None = None,
        active: bool | None = None,
    ) -> WebhookEndpoint:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            endpoint = await repos.webhooks.endpoint(endpoint_id)
            if endpoint is None:
                raise NotFoundError("webhook not found")
            if url is not None:
                endpoint.url = check_url(url, allow_http=self._allow_http)
            if events is not None:
                endpoint.events = check_events(events)
            if description is not None:
                endpoint.description = description.strip() or None
            if active is not None:
                endpoint.active = active
                if active:
                    endpoint.consecutive_failures = 0
                    endpoint.disabled_reason = None
            await repos.webhooks.save_endpoint(endpoint)
            await self._audit(repos, actor, "WEBHOOK_UPDATED", endpoint.id, reason, {"active": endpoint.active})
        return endpoint

    async def rotate_secret(self, actor: CurrentUser, endpoint_id: UUID, *, reason: str) -> str:
        secret = _new_secret()
        sealed = self._box.seal(actor.tenant_id, f"{SECRET_PURPOSE}:{endpoint_id}", secret.encode())
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.webhooks.endpoint(endpoint_id) is None:
                raise NotFoundError("webhook not found")
            await repos.webhooks.set_secret(endpoint_id, sealed, secret[:10] + "…")
            await self._audit(repos, actor, "WEBHOOK_SECRET_ROTATED", endpoint_id, reason, {})
        return secret

    async def delete(self, actor: CurrentUser, endpoint_id: UUID, *, reason: str) -> None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            if await repos.webhooks.endpoint(endpoint_id) is None:
                raise NotFoundError("webhook not found")
            await repos.webhooks.delete_endpoint(endpoint_id)
            await self._audit(repos, actor, "WEBHOOK_DELETED", endpoint_id, reason, {})

    async def secrets_for_url(self, tenant_id: UUID, url: str) -> list[bytes]:
        """Секреты подписок клиента на этот адрес — тестовый приёмник проверяет подпись пришедших запросов."""
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            result: list[bytes] = []
            for endpoint in await repos.webhooks.endpoints():
                if endpoint.url == url:
                    sealed = await repos.webhooks.secret(endpoint.id)
                    if sealed is not None:
                        result.append(self._box.open(tenant_id, f"{SECRET_PURPOSE}:{endpoint.id}", sealed))
            return result

    # ---------- доставки ----------
    async def deliveries(
        self, actor: CurrentUser, *, endpoint_id: UUID | None = None, status: str | None = None, limit: int = 50
    ) -> list[Delivery]:
        async with self._uow_factory(actor.tenant_id) as uow:
            return await self._repos(uow).webhooks.deliveries(
                endpoint_id=endpoint_id, status=status, limit=min(limit, MAX_LIST)
            )

    async def redeliver(self, actor: CurrentUser, delivery_id: UUID, *, reason: str) -> None:
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            delivery = await repos.webhooks.delivery(delivery_id)
            if delivery is None:
                raise NotFoundError("delivery not found")
            await repos.webhooks.requeue(delivery_id, self._clock.now())
            await self._audit(repos, actor, "WEBHOOK_REDELIVERED", delivery_id, reason, {})

    async def send_test(self, actor: CurrentUser, endpoint_id: UUID) -> Delivery:
        """Тестовое событие сразу (без очереди): видно, доходит ли и что ответил получатель."""
        now = self._clock.now()
        async with self._uow_factory(actor.tenant_id) as uow:
            repos = self._repos(uow)
            endpoint = await repos.webhooks.endpoint(endpoint_id)
            sealed = await repos.webhooks.secret(endpoint_id)
            if endpoint is None or sealed is None:
                raise NotFoundError("webhook not found")
            delivery_id = uuid4()
            delivery = Delivery(
                id=delivery_id,
                tenant_id=actor.tenant_id,
                endpoint_id=endpoint_id,
                event_type=TEST_EVENT,
                event_id=uuid4(),
                payload=envelope(
                    delivery_id=delivery_id,
                    public_event=TEST_EVENT,
                    event_id=delivery_id,
                    occurred_at=now,
                    tenant_id=actor.tenant_id,
                    data={"message": "Тестовое событие ANPR Parking", "requested_by": str(actor.user_id)},
                ),
                status=DeliveryStatus.PENDING,
                attempts=0,
                created_at=now,
                next_attempt_at=None,  # диспетчер тест не подхватит — отправляем здесь
            )
            await repos.webhooks.add_delivery(delivery)
        result = await self._send(delivery, endpoint.url, sealed)
        final = DeliveryStatus.DELIVERED if result.ok else DeliveryStatus.DEAD
        await self._queue.record(
            delivery.id,
            status=final,
            attempts=1,
            next_attempt_at=None,
            http_status=result.status,
            error=result.error,
            latency_ms=result.latency_ms,
            delivered_at=self._clock.now() if result.ok else None,
        )
        delivery.status, delivery.attempts = final, 1
        delivery.last_status, delivery.last_error, delivery.last_latency_ms = (
            result.status,
            result.error,
            result.latency_ms,
        )
        return delivery

    async def enqueue(
        self, tenant_id: UUID, internal_type: str, event_id: UUID, occurred_at: datetime, data: dict[str, Any]
    ) -> int:
        """Событие из стрима → доставки подписанным адресам. Повтор события не создаёт вторую доставку."""
        public = PUBLIC_NAME.get(internal_type)
        if public is None:
            return 0
        now = self._clock.now()
        created = 0
        async with self._uow_factory(tenant_id) as uow:
            repos = self._repos(uow)
            for endpoint in await repos.webhooks.subscribed(public):
                delivery_id = uuid4()
                delivery = Delivery(
                    id=delivery_id,
                    tenant_id=tenant_id,
                    endpoint_id=endpoint.id,
                    event_type=public,
                    event_id=event_id,
                    payload=envelope(
                        delivery_id=delivery_id,
                        public_event=public,
                        event_id=event_id,
                        occurred_at=occurred_at,
                        tenant_id=tenant_id,
                        data=data,
                    ),
                    status=DeliveryStatus.PENDING,
                    attempts=0,
                    created_at=now,
                    next_attempt_at=now,
                )
                created += int(await repos.webhooks.add_delivery(delivery))
        return created

    async def deliver_due(self, *, limit: int = 50) -> int:
        """Один проход диспетчера: вернуть число попыток."""
        now = self._clock.now()
        due = await self._queue.claim(now, now + LEASE, limit)
        for item in due:
            d = item.delivery
            result = await self._send(d, item.url, item.secret_encrypted)
            attempts = d.attempts + 1
            done = self._clock.now()
            if result.ok:
                status, retry_at = DeliveryStatus.DELIVERED, None
            else:
                retry_at = next_attempt(attempts, done)
                status = DeliveryStatus.RETRY if retry_at is not None else DeliveryStatus.DEAD
                log.warning(
                    "webhook_delivery_failed",
                    delivery_id=str(d.id),
                    attempts=attempts,
                    status=result.status,
                    error=result.error,
                    dead=status == DeliveryStatus.DEAD,
                )
            metrics.webhook_delivery_total.labels(result=status.lower()).inc()
            await self._queue.record(
                d.id,
                status=status,
                attempts=attempts,
                next_attempt_at=retry_at,
                http_status=result.status,
                error=result.error,
                latency_ms=result.latency_ms,
                delivered_at=done if result.ok else None,
            )
        return len(due)

    async def _send(self, delivery: Delivery, url: str, sealed_secret: bytes) -> SendResult:
        try:
            secret = self._box.open(delivery.tenant_id, f"{SECRET_PURPOSE}:{delivery.endpoint_id}", sealed_secret)
        except Exception as exc:  # ключ клиента недоступен — доставка ждёт следующей попытки
            return SendResult(ok=False, status=None, latency_ms=0, error=f"secret unavailable: {type(exc).__name__}")
        body = _body(delivery.payload)
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ANPR-Parking-Webhooks/1.0",
            "X-Parking-Event": delivery.event_type,
            "X-Parking-Delivery": str(delivery.id),
            "X-Parking-Timestamp": str(timestamp),
            "X-Parking-Signature": sign(secret, timestamp, body),
        }
        try:
            return await self._sender.send(url, body, headers)
        except ValidationError as exc:  # адрес не прошёл SSRF-проверку при отправке
            return SendResult(ok=False, status=None, latency_ms=0, error=exc.message)

    @staticmethod
    async def _audit(
        repos: WebhookRepositories,
        actor: CurrentUser,
        action: str,
        entity_id: UUID,
        reason: str,
        payload: dict[str, object],
    ) -> None:
        await repos.audit.record(
            AuditEntry(
                tenant_id=actor.tenant_id,
                action=action,
                entity_type="webhook",
                entity_id=entity_id,
                actor_type=ActorType.USER,
                actor_id=actor.user_id,
                user_id=actor.user_id,
                reason=reason,
                payload=payload,
            )
        )
