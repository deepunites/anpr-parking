"""Что Tariff отдаёт другим модулям: расчёт по версии, действующей в момент выезда."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.tariff.domain.engine import AccessKind, AccessTerms, BreakdownLine, TariffCalculation
from parking.modules.tariff.domain.rules import RoundingMode, TariffRules

__all__ = [
    "AccessKind",
    "AccessTerms",
    "BreakdownLine",
    "RoundingMode",
    "TariffCalculation",
    "TariffCalculator",
    "TariffPlanView",
    "TariffRules",
    "TariffVersionView",
]


class TariffCalculator(Protocol):
    async def calculate(self, lot_id: UUID, entry: datetime, exit: datetime, access: AccessTerms) -> TariffCalculation:
        """Внутри tenant-контекста. Версия — действующая в момент `exit`; `tariff_plan_version_id` проставлен.
        Бросает NotFoundError, если у площадки нет действующего тарифа."""
        ...


@dataclass(frozen=True, slots=True)
class TariffPlanView:
    id: UUID
    lot_id: UUID
    code: str
    name: str
    is_default: bool
    active: bool


@dataclass(frozen=True, slots=True)
class TariffVersionView:
    id: UUID
    plan_id: UUID
    version: int
    rules: dict[str, Any]
    valid_from: datetime
    valid_to: datetime | None
    holiday_calendar_id: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HolidayCalendarView:
    id: UUID
    name: str
    days: frozenset[date]
