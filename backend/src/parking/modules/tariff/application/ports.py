from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.tariff.application.contracts import TariffPlanView, TariffVersionView


@dataclass(frozen=True, slots=True)
class VersionRecord:
    """Действующая версия тарифа вместе с окном действия и праздниками календаря — одно чтение на EXIT."""

    id: UUID
    plan_id: UUID
    lot_id: UUID
    version: int
    rules: dict[str, Any]
    holiday_calendar_id: UUID | None
    valid_from: datetime
    valid_to: datetime | None
    holidays: frozenset[date]

    def covers(self, at: datetime) -> bool:
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)


class TariffRepository(Protocol):
    async def default_version_at(self, lot_id: UUID, at: datetime) -> VersionRecord | None: ...
    async def list_plans(self, lot_id: UUID) -> list[TariffPlanView]: ...
    async def get_plan(self, plan_id: UUID) -> TariffPlanView | None: ...
    async def create_plan(
        self, *, plan_id: UUID, tenant_id: UUID, lot_id: UUID, code: str, name: str, is_default: bool
    ) -> None: ...
    async def list_versions(self, plan_id: UUID) -> list[TariffVersionView]: ...
    async def close_open_version(self, plan_id: UUID, at: datetime) -> None:
        """Закрывает открытую (без верхней границы) версию плана в момент `at`; нет открытой — ничего."""
        ...

    async def add_version(
        self,
        *,
        version_id: UUID,
        tenant_id: UUID,
        plan_id: UUID,
        rules: dict[str, Any],
        valid_from: datetime,
        valid_to: datetime | None,
        holiday_calendar_id: UUID | None,
        created_by: UUID,
    ) -> int: ...
