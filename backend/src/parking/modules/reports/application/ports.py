from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LotScope:
    """Фильтр площадок: выбранная площадка и/или площадки, доступные актору (None — все площадки клиента)."""

    lot_id: UUID | None
    lots: frozenset[UUID] | None


@dataclass(frozen=True, slots=True)
class TenantInfo:
    timezone: str
    currency: str
    plan_code: str
    plan_name: str
    max_lots: int | None
    max_places: int | None
    max_cameras: int | None
    lots: int
    places: int
    cameras: int


@dataclass(frozen=True, slots=True)
class RevenueRow:
    day: date
    method: str
    amount: int
    count: int


@dataclass(frozen=True, slots=True)
class Totals:
    gross: int
    refunds: int
    payments: int  # платежей с суммой > 0
    entries: int
    exits: int
    avg_minutes: float | None
    review: int


@dataclass(frozen=True, slots=True)
class LotRow:
    lot_id: UUID
    name: str
    capacity: int
    revenue: int
    entries: int


@dataclass(frozen=True, slots=True)
class HourRow:
    hour: int
    entries: int
    exits: int


@dataclass(frozen=True, slots=True)
class ManualRow:
    user_id: UUID | None
    name: str
    role: str | None
    action: str
    count: int


@dataclass(frozen=True, slots=True)
class ShiftRow:
    shift_id: UUID
    lot_name: str
    cashier_name: str
    opened_at: datetime
    closed_at: datetime
    expected: int
    counted: int
    discrepancy: int


@dataclass(frozen=True, slots=True)
class CameraRow:
    camera_id: UUID
    name: str
    lot_name: str
    status: str


@dataclass(frozen=True, slots=True)
class StatusPoint:
    camera_id: UUID
    changed_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class BarrierRow:
    barrier_id: UUID
    name: str
    lot_name: str
    commands: int
    failed: int
    avg_latency_ms: int | None


class OwnerQueries(Protocol):
    """Чтение отчётов клиента (пул bulk, RLS по tenant). Границы периода — [a, b) в UTC."""

    async def tenant(self, tenant_id: UUID) -> TenantInfo: ...
    async def revenue(self, scope: LotScope, a: datetime, b: datetime, tz: str) -> list[RevenueRow]: ...
    async def totals(self, scope: LotScope, a: datetime, b: datetime) -> Totals: ...
    async def lots(self, scope: LotScope, a: datetime, b: datetime) -> list[LotRow]: ...
    async def hours(self, scope: LotScope, a: datetime, b: datetime, tz: str) -> list[HourRow]: ...
    async def manual(self, scope: LotScope, a: datetime, b: datetime) -> list[ManualRow]: ...
    async def shifts(self, scope: LotScope, a: datetime, b: datetime) -> list[ShiftRow]: ...
    async def cameras(self, scope: LotScope) -> list[CameraRow]: ...
    async def status_history(
        self, scope: LotScope, a: datetime, b: datetime
    ) -> tuple[dict[UUID, str], list[StatusPoint]]: ...
    async def barriers(self, scope: LotScope, a: datetime, b: datetime) -> list[BarrierRow]: ...


@dataclass(frozen=True, slots=True)
class PlatformTenant:
    tenant_id: UUID
    name: str
    plan_code: str
    price: int | None
    status: str
    trial_ends_at: datetime | None
    paid_until: datetime | None
    created_at: datetime
    lots: int
    cameras: int
    cameras_offline: int
    sessions_7d: int


@dataclass(frozen=True, slots=True)
class LocalHealth:
    outbox_pending: int
    outbox_oldest_seconds: int | None
    webhooks_dead_24h: int
    cameras_offline: int
    cameras_total: int


class PlatformQueries(Protocol):
    """Кросс-tenant агрегаты для владельца платформы (роль maintenance): без данных водителей."""

    async def tenants(self, now: datetime) -> list[PlatformTenant]: ...
    async def signups(self, since: datetime) -> list[tuple[date, int]]: ...
    async def local_health(self, now: datetime) -> LocalHealth: ...


@dataclass(frozen=True, slots=True)
class PromHealth:
    api_up: int | None
    api_p95_ms: float | None
    api_error_rate: float | None
    exit_p95_ms: float | None
    outbox_lag_seconds: float | None
    firing_alerts: list[str]


class HealthSource(Protocol):
    """Prometheus (если настроен). None — источник не настроен или не ответил: дашборд показывает локальные данные."""

    async def snapshot(self) -> PromHealth | None: ...
