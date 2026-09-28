from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]  # поля формы presigned POST: policy, signature, key, Content-Type…
    object_key: str
    expires_in: int
    max_bytes: int


class ObjectStore(Protocol):
    async def presign_upload(self, key: str, *, max_bytes: int, ttl_seconds: int) -> PresignedUpload:
        """Presigned POST: Content-Type зафиксирован image/jpeg, content-length-range ≤ max_bytes."""
        ...

    async def presign_get(self, key: str, *, ttl_seconds: int) -> str: ...
    async def put_object(self, key: str, data: bytes) -> None: ...


class SessionImageSource(Protocol):
    async def images_for_session(self, session_id: UUID) -> list[tuple[UUID, str, datetime]]:
        """(event_id, object_key, event_time) — реализует репозиторий anpr_event (структурно)."""
        ...


class MediaRepository(Protocol):
    async def add_expected(self, *, key: str, tenant_id: UUID, lot_id: UUID, camera_id: UUID, at: datetime) -> None: ...
    async def mark_delivered(self, key: str, *, size_bytes: int, at: datetime) -> None: ...


class PendingFrames(Protocol):
    """Кадры, которые не удалось положить в объектное хранилище (MinIO недоступен): ключ уже выдан событию,
    байты ждут повторной отправки (§5: «изображение помечено как недоставленное, повтор»)."""

    async def put(self, key: str, data: bytes, *, ttl_seconds: int) -> None: ...
    async def pop_batch(self, limit: int) -> list[tuple[str, bytes]]: ...
    async def requeue(self, key: str, data: bytes, *, ttl_seconds: int) -> None: ...
    async def length(self) -> int: ...
