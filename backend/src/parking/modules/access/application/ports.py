from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from parking.modules.access.domain.rules import AccessDecision, AccessKind, AccessRule


class AccessRuleRepository(Protocol):
    async def rules_for(self, plate_hash: bytes, lot_id: UUID, at: datetime) -> list[AccessRule]:
        """Один индексный запрос: правила номера, применимые к площадке (или глобальные) и действующие в `at`."""
        ...

    async def add(
        self, rule: AccessRule, plate_encrypted: bytes, enc_key_version: int, created_by: UUID | None
    ) -> None: ...
    async def get(self, rule_id: UUID) -> AccessRule | None: ...
    async def revoke(self, rule_id: UUID, *, revoked_by: UUID, reason: str, at: datetime) -> bool: ...
    async def list(
        self, *, kind: AccessKind | None, lot_id: UUID | None, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[tuple[AccessRule, bytes, int, datetime]]:
        """(rule, plate_encrypted, enc_key_version, created_at)."""
        ...


class DecisionCache(Protocol):
    async def get(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes) -> AccessDecision | None: ...
    async def set(
        self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, decision: AccessDecision, ttl_seconds: int
    ) -> None: ...
    async def invalidate(self, tenant_id: UUID, plate_hash: bytes) -> None:
        """Сбрасывает решения номера по всем площадкам tenant и оповещает другие процессы."""
        ...


@dataclass(frozen=True, slots=True)
class SubscriptionRecord:
    id: UUID
    lot_id: UUID | None
    plan_code: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int
    plate_encrypted: bytes
    enc_key_version: int


class SubscriptionRepository(Protocol):
    async def get(self, subscription_id: UUID) -> SubscriptionRecord | None:
        """Абонемент и шифртекст номера (из его правила доступа) — для чека."""
        ...

    async def add(
        self,
        *,
        subscription_id: UUID,
        tenant_id: UUID,
        lot_id: UUID | None,
        plate_hash: bytes,
        plan_code: str,
        starts_at: datetime,
        ends_at: datetime,
        price_minor: int,
        created_by: UUID,
    ) -> None: ...
