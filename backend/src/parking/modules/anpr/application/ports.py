from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.access.application.contracts import AccessDecision
from parking.modules.anpr.domain.event import CameraEvent
from parking.modules.devices.application.contracts import BarrierInfoCache
from parking.platform.db.uow import UnitOfWork

__all__ = [
    "AnprEventRepository",
    "BarrierInfoCache",
    "BufferedEntry",
    "CachedAccess",
    "DlqEntry",
    "DlqStore",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "OfflineBuffer",
    "RateLimiter",
    "ResponseCache",
    "StoredEvent",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredEvent:
    event: CameraEvent
    plate_hash: bytes
    plate_encrypted: bytes
    hmac_key_version: int
    enc_key_version: int
    result: str
    reject_reason: str | None
    session_id: UUID | None
    saga_id: UUID | None
    idempotency_key: bytes
    late: bool


class AnprEventRepository(Protocol):
    async def add(self, stored: StoredEvent) -> None: ...
    async def images_for_session(self, session_id: UUID) -> list[tuple[UUID, str, datetime]]: ...


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    event_id: UUID | None
    result: str
    response: dict[str, Any]


class IdempotencyRepository(Protocol):
    async def lock(self, key: bytes) -> None:
        """pg_advisory_xact_lock по ключу: параллельные одинаковые запросы сериализуются до любой работы."""
        ...

    async def find(self, key: bytes, event_second: datetime) -> IdempotencyRecord | None: ...
    async def add(
        self,
        key: bytes,
        event_second: datetime,
        *,
        tenant_id: UUID,
        camera_id: UUID,
        event_id: UUID | None,
        result: str,
        response: dict[str, Any],
    ) -> None: ...


class ResponseCache(Protocol):
    """Redis — только ускорение повторов; источник истины — anpr_idempotency."""

    async def get(self, key: bytes) -> dict[str, Any] | None: ...
    async def set(self, key: bytes, response: dict[str, Any], ttl_seconds: int) -> None: ...


class RateLimiter(Protocol):
    async def hit(self, subject: str, *, limit: int, window_seconds: int) -> int | None:
        """None — в пределах лимита; иначе — сколько секунд ждать (Retry-After)."""
        ...


@dataclass(frozen=True, slots=True)
class BufferedEntry:
    entry_id: str
    fields: dict[str, str]


class OfflineBuffer(Protocol):
    async def enqueue(self, fields: dict[str, str]) -> str: ...
    async def length(self) -> int: ...
    async def ensure_group(self) -> None: ...
    async def read(self, consumer: str, *, count: int, own_pending: bool) -> list[BufferedEntry]: ...
    async def claim_stale(self, consumer: str, *, min_idle_ms: int, count: int) -> list[BufferedEntry]: ...
    async def ack(self, entry_ids: Sequence[str]) -> None: ...
    async def to_dlq(self, entry: BufferedEntry, error: str) -> None: ...
    async def pending(self) -> int: ...


@dataclass(frozen=True, slots=True)
class DlqEntry:
    entry_id: str
    stream: str
    fields: dict[str, str]


class DlqStore(Protocol):
    async def entries(self, stream: str, *, limit: int) -> list[DlqEntry]: ...
    async def replay(self, stream: str, entry_ids: Sequence[str] | None, *, dry_run: bool) -> list[DlqEntry]:
        """Возвращает записи, которые (были бы) возвращены в исходный стрим; при dry_run ничего не меняет."""
        ...


class CachedAccess(Protocol):
    async def resolve(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime) -> AccessDecision: ...
    async def resolve_in(
        self, uow: UnitOfWork, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime
    ) -> AccessDecision:
        """Кеши → правила в транзакции вызывающего (одна транзакция на событие)."""
        ...

    async def cached(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes) -> AccessDecision | None:
        """Только кеши (без БД) — для оффлайн-режима, когда PostgreSQL недоступен."""
        ...
