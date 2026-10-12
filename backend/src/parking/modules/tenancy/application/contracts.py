"""Что tenancy отдаёт другим модулям: справочник площадок с политиками."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from parking.modules.tenancy.domain.lot import LotPolicies, ParkingLot, UnknownExitPolicy
from parking.modules.tenancy.domain.subscription import Plan, TenantStatus

__all__ = ["LotDirectory", "LotInfo", "LotPolicies", "LotView", "Plan", "TenantStatus", "UnknownExitPolicy"]


@dataclass(frozen=True, slots=True)
class LotInfo:
    id: UUID
    tenant_id: UUID
    code: str
    timezone: str
    capacity: int
    active: bool
    policies: LotPolicies


class LotDirectory(Protocol):
    async def get(self, lot_id: UUID) -> LotInfo | None:
        """Внутри tenant-контекста UoW."""
        ...

    async def shows_full_plates(self) -> bool:
        """Хоть на одной действующей площадке клиента включены полные номера (экраны без площадки — карточка машины)."""
        ...


@dataclass(frozen=True, slots=True)
class LotView:
    id: UUID
    code: str
    name: str
    address: str | None
    capacity: int
    timezone: str
    active: bool
    policies: LotPolicies

    @classmethod
    def of(cls, lot: ParkingLot) -> LotView:
        return cls(
            id=lot.id,
            code=lot.code,
            name=lot.name,
            address=lot.address,
            capacity=lot.capacity,
            timezone=lot.timezone,
            active=lot.active,
            policies=lot.policies,
        )


class SubscriptionGate(Protocol):
    """Лимиты тарифа для других модулей (devices считает свои камеры сам и спрашивает, можно ли ещё одну)."""

    async def ensure(self, tenant_id: UUID, resource: str, used_after: int) -> None:
        """Бросает ConflictError(SUBSCRIPTION_INACTIVE | PLAN_LIMIT_REACHED)."""
        ...
