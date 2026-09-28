"""Порты интеграций. Реализации — infrastructure/; fake — только в tests/."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.integrations.domain.api_key import ApiKey
from parking.modules.integrations.domain.journal import JournalEntry
from parking.modules.integrations.domain.webhook import Delivery, WebhookEndpoint


@dataclass(frozen=True, slots=True)
class ApiKeyLookup:
    id: UUID
    tenant_id: UUID
    name: str
    key_hmac: bytes
    scopes: tuple[str, ...]
    lot_scope: frozenset[UUID]
    expires_at: datetime | None
    revoked_at: datetime | None
    tenant_active: bool
    created_by: UUID
    creator_active: bool


class ApiKeyRepository(Protocol):
    async def add(self, key: ApiKey, key_hmac: bytes) -> None: ...
    async def list(self) -> list[ApiKey]: ...
    async def get_for_update(self, key_id: UUID) -> ApiKey | None: ...
    async def revoke(self, key_id: UUID, *, at: datetime, by: UUID) -> None: ...
    async def lookup(self, prefix: str) -> ApiKeyLookup | None:
        """Pre-tenant (SECURITY DEFINER функция)."""
        ...

    async def touch(self, key_id: UUID, at: datetime) -> None: ...


class WebhookRepository(Protocol):
    async def add_endpoint(self, endpoint: WebhookEndpoint, secret_encrypted: bytes) -> None: ...
    async def endpoints(self) -> list[WebhookEndpoint]: ...
    async def endpoint(self, endpoint_id: UUID) -> WebhookEndpoint | None: ...
    async def save_endpoint(self, endpoint: WebhookEndpoint) -> None: ...
    async def set_secret(self, endpoint_id: UUID, secret_encrypted: bytes, hint: str) -> None: ...
    async def secret(self, endpoint_id: UUID) -> bytes | None: ...
    async def delete_endpoint(self, endpoint_id: UUID) -> None: ...
    async def subscribed(self, public_event: str) -> list[WebhookEndpoint]: ...
    async def add_delivery(self, delivery: Delivery) -> bool:
        """False — эта доставка уже есть (повтор события из стрима)."""
        ...

    async def deliveries(self, *, endpoint_id: UUID | None, status: str | None, limit: int) -> list[Delivery]: ...
    async def delivery(self, delivery_id: UUID) -> Delivery | None: ...
    async def requeue(self, delivery_id: UUID, at: datetime) -> None: ...
    async def stats(self) -> dict[UUID, dict[str, Any]]:
        """endpoint_id → {last_status, last_at, delivered_24h, failed_24h}."""
        ...


@dataclass(frozen=True, slots=True)
class DueDelivery:
    delivery: Delivery
    url: str
    secret_encrypted: bytes


class DeliveryQueue(Protocol):
    """Диспетчер поверх всех клиентов (роль maintenance): забрать срок наступивших и записать итог попытки."""

    async def claim(self, now: datetime, lease_until: datetime, limit: int) -> list[DueDelivery]: ...
    async def record(
        self,
        delivery_id: UUID,
        *,
        status: str,
        attempts: int,
        next_attempt_at: datetime | None,
        http_status: int | None,
        error: str | None,
        latency_ms: int | None,
        delivered_at: datetime | None,
    ) -> None: ...


class SecretBox(Protocol):
    def seal(self, tenant_id: UUID, purpose: str, secret: bytes) -> bytes: ...
    def open(self, tenant_id: UUID, purpose: str, blob: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    status: int | None
    latency_ms: int
    error: str | None


class WebhookSender(Protocol):
    async def send(self, url: str, body: bytes, headers: dict[str, str]) -> SendResult: ...


class JournalRepository(Protocol):
    async def add_many(self, entries: list[JournalEntry]) -> None: ...
    async def search(
        self,
        *,
        lot_ids: frozenset[UUID] | None,
        lot_id: UUID | None,
        outcome: str | None,
        correlation_id: UUID | None,
        camera_id: UUID | None,
        since: datetime,
        limit: int,
    ) -> list[JournalEntry]: ...
