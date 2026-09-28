"""v2 этап 7 «Дашборды»: сводка владельца парковки (выручка, сессии, загрузка, ручные действия, смены, устройства,
лимиты, сигналы) и сводка владельца платформы (клиенты, MRR, пробные, внимание, здоровье)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from parking.api.deps import get_container, permission_extra, require
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class KpiOut(_Out):
    value: float
    previous: float
    change: float | None = Field(description="доля изменения к прошлому периоду той же длины; null — не с чем сравнить")


class DayRevenueOut(_Out):
    day: date
    total: int
    by_method: dict[str, int]


class LotReportOut(_Out):
    lot_id: UUID
    name: str
    capacity: int
    revenue: int
    entries: int


class HourLoadOut(_Out):
    hour: int
    entries: float = Field(description="в среднем за день периода")
    exits: float


class OperatorManualOut(_Out):
    user_id: UUID | None
    name: str
    role: str | None
    opens: int
    entries: int
    corrections: int
    closes: int
    total: int


class ShiftDiscrepancyOut(_Out):
    shift_id: UUID
    lot_name: str
    cashier_name: str
    opened_at: datetime
    closed_at: datetime
    expected: int
    counted: int
    discrepancy: int


class CameraAvailabilityOut(_Out):
    camera_id: UUID
    name: str
    lot_name: str
    status: str
    availability: float | None = Field(description="доля времени периода, когда камера принимала события")


class BarrierReportOut(_Out):
    barrier_id: UUID
    name: str
    lot_name: str
    commands: int
    failed: int
    avg_latency_ms: int | None


class LimitOut(_Out):
    kind: str
    used: int
    limit: int | None


class SignalOut(_Out):
    code: str = Field(description="CASH_DISCREPANCY | MANY_MANUAL_OPENS | DEVICE_UNAVAILABLE | LIMIT_NEAR")
    severity: str
    params: dict[str, Any]


class OwnerDashboardOut(_Out):
    period_start: date
    period_end: date
    timezone: str
    currency: str
    lot_id: UUID | None
    revenue: KpiOut = Field(description="выручка за вычетом возвратов, минорные единицы")
    sessions: KpiOut = Field(description="въезды за период")
    avg_check: KpiOut
    payments: int
    refunds: int
    exits: int
    avg_minutes: float | None
    review: int = Field(description="сессий на разборе сейчас")
    revenue_by_day: list[DayRevenueOut]
    revenue_by_method: dict[str, int]
    lots: list[LotReportOut]
    load_by_hour: list[HourLoadOut]
    manual: list[OperatorManualOut]
    shifts: list[ShiftDiscrepancyOut]
    cameras: list[CameraAvailabilityOut]
    barriers: list[BarrierReportOut]
    plan_code: str
    plan_name: str
    limits: list[LimitOut]
    signals: list[SignalOut]


@router.get("/owner", response_model=OwnerDashboardOut, openapi_extra=permission_extra(Permission.DASHBOARD_VIEW))
async def owner_dashboard(
    user: Annotated[CurrentUser, Depends(require(Permission.DASHBOARD_VIEW))],
    container: Annotated[Container, Depends(get_container)],
    start: Annotated[date | None, Query(alias="from", description="первый день (местная дата клиента)")] = None,
    end: Annotated[date | None, Query(alias="to", description="последний день включительно")] = None,
    lot_id: UUID | None = None,
    days: Annotated[int, Query(ge=1, le=92, description="длина периода, если from не задан")] = 7,
) -> OwnerDashboardOut:
    """По умолчанию — последние 7 дней по часовому поясу клиента; период до 92 дней; сравнение с прошлым периодом."""
    board = await container.owner_dashboard.build(user, start=start, end=end, lot_id=lot_id, days=days)
    return OwnerDashboardOut.model_validate(board)


class AttentionOut(_Out):
    tenant_id: UUID
    name: str
    status: str
    plan_code: str
    reasons: list[str] = Field(
        description="SUSPENDED | TRIAL_EXPIRED | TRIAL_ENDING | PAYMENT_OVERDUE | CAMERAS_OFFLINE | NO_TRAFFIC"
    )
    trial_ends_at: datetime | None
    paid_until: datetime | None


class SignupWeekOut(BaseModel):
    week: date
    count: int


class LocalHealthOut(_Out):
    outbox_pending: int
    outbox_oldest_seconds: int | None
    webhooks_dead_24h: int
    cameras_offline: int
    cameras_total: int


class PromHealthOut(_Out):
    api_up: int | None
    api_p95_ms: float | None
    api_error_rate: float | None
    exit_p95_ms: float | None
    outbox_lag_seconds: float | None
    firing_alerts: list[str]


class PlatformDashboardOut(_Out):
    generated_at: datetime
    clients: dict[str, int]
    mrr: int = Field(description="сумма месячных цен тарифов активных клиентов, минорные единицы")
    contract_clients: int = Field(description="активные клиенты на тарифе без цены (по договору) — не в MRR")
    trials: int
    trials_ending: int
    signups_by_week: list[SignupWeekOut]
    by_plan: dict[str, int]
    attention: list[AttentionOut]
    health_source: str = Field(description="prometheus | local")
    local: LocalHealthOut
    prometheus: PromHealthOut | None
    dlq_length: int | None


@router.get("/platform", response_model=PlatformDashboardOut, openapi_extra=permission_extra(Permission.PLATFORM_VIEW))
async def platform_dashboard(
    _user: Annotated[CurrentUser, Depends(require(Permission.PLATFORM_VIEW))],
    container: Annotated[Container, Depends(get_container)],
) -> PlatformDashboardOut:
    """Агрегаты по клиентам без данных водителей; здоровье — из Prometheus, если настроен (PARKING_PROMETHEUS_URL)."""
    board = await container.platform_dashboard.build()
    data = {f: getattr(board, f) for f in PlatformDashboardOut.model_fields if f != "signups_by_week"}
    data["signups_by_week"] = [SignupWeekOut(week=w, count=n) for w, n in board.signups_by_week]
    return PlatformDashboardOut.model_validate(data)
