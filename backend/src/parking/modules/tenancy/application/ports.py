from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.tenancy.domain.layout import Spot
from parking.modules.tenancy.domain.lot import ParkingLot
from parking.modules.tenancy.domain.subscription import Plan, TenantStatus, TenantSubscription


@dataclass(frozen=True, slots=True)
class LayoutRecord:
    lot_id: UUID
    version: int
    document: dict[str, Any]
    spots: int
    created_by: UUID | None
    reason: str | None
    created_at: datetime


class LayoutRepository(Protocol):
    """v2 этап 5: версии схемы площадки (в tenant-контексте UoW)."""

    async def current(self, lot_id: UUID) -> LayoutRecord | None: ...
    async def version(self, lot_id: UUID, version: int) -> LayoutRecord | None: ...
    async def history(self, lot_id: UUID, *, limit: int) -> list[LayoutRecord]: ...
    async def add(
        self,
        *,
        tenant_id: UUID,
        lot_id: UUID,
        version: int,
        document: dict[str, Any],
        spots: int,
        created_by: UUID,
        reason: str | None,
    ) -> None: ...
    async def sync_spots(
        self, tenant_id: UUID, lot_id: UUID, spots: tuple[Spot, ...], zone_names: dict[str, str]
    ) -> None: ...


class LotRepository(Protocol):
    async def get(self, lot_id: UUID) -> ParkingLot | None: ...
    async def get_for_update(self, lot_id: UUID) -> ParkingLot | None: ...
    async def list(self, *, only: frozenset[UUID] | None) -> list[ParkingLot]: ...
    async def add(self, lot: ParkingLot) -> None: ...
    async def save(self, lot: ParkingLot) -> None: ...


# ---- v2 этап 3: подписка, регистрация, владелец платформы ----
class SubscriptionRepository(Protocol):
    async def plans(self) -> list[Plan]: ...
    async def plan(self, code: str) -> Plan | None: ...
    async def get(self, tenant_id: UUID) -> TenantSubscription | None: ...
    async def set_plan(self, tenant_id: UUID, plan_code: str) -> None: ...
    async def admin_update(
        self,
        tenant_id: UUID,
        *,
        status: TenantStatus | None,
        plan_code: str | None,
        paid_until: datetime | None,
        trial_ends_at: datetime | None,
    ) -> None: ...
    async def create_tenant(
        self,
        *,
        tenant_id: UUID,
        code: str,
        name: str,
        plan_code: str,
        trial_ends_at: datetime,
        contact_phone: str | None,
    ) -> None: ...
    async def add_wrapped_keys(
        self, tenant_id: UUID, version: int, wrapped_hmac: bytes, wrapped_dek: bytes, kek_id: str
    ) -> None: ...
    async def usage(self, tenant_id: UUID) -> tuple[int, int]:
        """(число активных площадок, сумма мест) — камеры считает devices."""
        ...


@dataclass(frozen=True, slots=True)
class SignupRecord:
    signup_id: str
    company: str
    name: str
    email: str
    phone: str | None
    password_hash: str
    plan_code: str
    locale: str
    code_hash: str
    attempts: int
    expires_at: datetime


class SignupStore(Protocol):
    async def save(self, record: SignupRecord, ttl_seconds: int) -> None: ...
    async def get(self, signup_id: str) -> SignupRecord | None: ...
    async def bump_attempts(self, signup_id: str) -> int: ...
    async def delete(self, signup_id: str) -> None: ...
    async def hit_ip(self, ip: str, window_seconds: int) -> int:
        """Счётчик заявок с IP за окно (защита от массовой регистрации)."""
        ...


class CodeSender(Protocol):
    channel: str

    async def send(self, *, phone: str | None, email: str, code: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PlatformTenantRow:
    id: UUID
    code: str
    name: str
    plan_code: str
    status: str
    trial_ends_at: datetime | None
    paid_until: datetime | None
    created_at: datetime
    contact_phone: str | None
    lots: int
    places: int
    cameras: int
    users: int
    sessions_30d: int


class PlatformDirectory(Protocol):
    """Кросс-tenant чтение для владельца платформы (роль maintenance): только агрегаты, без данных водителей."""

    async def list_tenants(self) -> list[PlatformTenantRow]: ...
