"""Подписка клиента на платформу (v2 этап 3): тариф с лимитами, пробный период, статус.

Правило: подписка ограничивает только НАСТРОЙКУ (новые площадки, места, камеры), но никогда не проезд машин —
водитель не должен застрять у шлагбаума из-за неоплаченного счёта платформы (A-82)."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from parking.shared.errors import ConflictError


class TenantStatus(enum.StrEnum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class Resource(enum.StrEnum):
    LOTS = "lots"
    PLACES = "places"
    CAMERAS = "cameras"


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    name: str
    max_lots: int | None
    max_places: int | None
    max_cameras: int | None
    price_minor: int | None
    trial_days: int
    sort_order: int = 0

    def limit(self, resource: Resource) -> int | None:
        return {Resource.LOTS: self.max_lots, Resource.PLACES: self.max_places, Resource.CAMERAS: self.max_cameras}[
            resource
        ]


@dataclass(frozen=True, slots=True)
class TenantSubscription:
    tenant_id: UUID
    plan: Plan
    status: TenantStatus
    trial_ends_at: datetime | None
    paid_until: datetime | None

    def trial_expired(self, now: datetime) -> bool:
        return self.status is TenantStatus.TRIAL and self.trial_ends_at is not None and self.trial_ends_at < now

    def blocked_reason(self, now: datetime) -> str | None:
        if self.status is TenantStatus.SUSPENDED:
            return "SUSPENDED"
        if self.trial_expired(now):
            return "TRIAL_EXPIRED"
        return None

    def ensure_can_configure(self, now: datetime) -> None:
        reason = self.blocked_reason(now)
        if reason is not None:
            raise ConflictError(
                "subscription is not active: new lots and cameras are unavailable",
                code="SUBSCRIPTION_INACTIVE",
                details={"reason": reason},
            )

    def ensure_within(self, resource: Resource, used_after: int, now: datetime) -> None:
        """used_after — сколько будет после действия. Превышение → 409 PLAN_LIMIT_REACHED с цифрами для экрана."""
        self.ensure_can_configure(now)
        limit = self.plan.limit(resource)
        if limit is not None and used_after > limit:
            raise ConflictError(
                f"plan limit reached: {resource.value}",
                code="PLAN_LIMIT_REACHED",
                details={"resource": resource.value, "limit": limit, "requested": used_after, "plan": self.plan.code},
            )
