"""Контракт для Parking/Tariff: единая точка разрешения доступа."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from parking.modules.access.domain.rules import AccessDecision, AccessKind

__all__ = ["AccessDecision", "AccessKind", "AccessResolver", "AccessRuleView"]


class AccessResolver(Protocol):
    async def resolve(self, tenant_id: UUID, lot_id: UUID, plate_hash: bytes, at: datetime) -> AccessDecision:
        """Кеш → БД (один запрос). Не бросает при недоступности Redis — деградирует к БД."""
        ...


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    """Проданный абонемент: открытый номер — только для чека и администратора, никогда не в лог."""

    id: UUID
    lot_id: UUID | None
    plate: str
    plan_code: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int


@dataclass(frozen=True, slots=True)
class AccessRuleView:
    id: UUID
    lot_id: UUID | None
    kind: AccessKind
    plate: str  # открытый номер — только для администратора списка, никогда не в лог
    valid_from: datetime
    valid_to: datetime | None
    discount_percent: int
    source_id: UUID | None
    reason: str | None
    revoked_at: datetime | None
    created_at: datetime
