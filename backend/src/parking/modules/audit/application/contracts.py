"""Open Host Service аудита: любой модуль пишет записи через этот порт в своей UoW (I-15)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


class ActorType(enum.StrEnum):
    USER = "USER"
    CAMERA = "CAMERA"
    SYSTEM = "SYSTEM"
    PROVIDER = "PROVIDER"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEntry:
    tenant_id: UUID
    action: str
    entity_type: str
    actor_type: ActorType = ActorType.SYSTEM
    actor_id: UUID | None = None
    user_id: UUID | None = None
    entity_id: UUID | None = None
    lot_id: UUID | None = None
    session_id: UUID | None = None
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ip_address: str | None = None
    user_agent: str | None = None


class AuditWriter(Protocol):
    async def record(self, entry: AuditEntry) -> None:
        """Пишет запись в той же транзакции, что и действие. Бросает исключения БД как есть."""
        ...
