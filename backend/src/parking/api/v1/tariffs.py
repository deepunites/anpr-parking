from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import MoneyOut, Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.tariff.application.contracts import (
    AccessKind,
    AccessTerms,
    TariffCalculation,
    TariffPlanView,
    TariffVersionView,
)
from parking.modules.tariff.application.service import PublishVersionCommand
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1", tags=["tariffs"])


class PlanOut(BaseModel):
    id: str
    lot_id: str
    code: str
    name: str
    is_default: bool
    active: bool

    @classmethod
    def of(cls, p: TariffPlanView) -> PlanOut:
        return cls(
            id=str(p.id), lot_id=str(p.lot_id), code=p.code, name=p.name, is_default=p.is_default, active=p.active
        )


class VersionOut(BaseModel):
    id: str
    plan_id: str
    version: int
    rules: dict[str, Any]
    valid_from: datetime
    valid_to: datetime | None
    holiday_calendar_id: str | None
    created_at: datetime

    @classmethod
    def of(cls, v: TariffVersionView) -> VersionOut:
        return cls(
            id=str(v.id),
            plan_id=str(v.plan_id),
            version=v.version,
            rules=v.rules,
            valid_from=v.valid_from,
            valid_to=v.valid_to,
            holiday_calendar_id=str(v.holiday_calendar_id) if v.holiday_calendar_id else None,
            created_at=v.created_at,
        )


class CreatePlanRequest(BaseModel):
    code: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    is_default: bool = False
    reason: Reason


class PublishVersionRequest(BaseModel):
    rules: dict[str, Any]
    valid_from: datetime
    valid_to: datetime | None = None
    holiday_calendar_id: UUID | None = None
    reason: Reason


class QuoteRequest(BaseModel):
    lot_id: UUID
    entry: datetime
    exit: datetime
    access_kind: AccessKind = AccessKind.NORMAL
    discount_percent: int = Field(default=0, ge=0, le=100)


class PreviewRequest(BaseModel):
    lot_id: UUID
    rules: dict[str, Any] = Field(description="правила тарифа в формате версии (суммы в тийинах)")
    start: datetime | None = Field(default=None, description="время въезда для примера; по умолчанию — сейчас")
    durations_minutes: list[Annotated[int, Field(ge=1, le=43_200)]] = Field(
        default=[30, 60, 180, 600, 1440], min_length=1, max_length=12, description="длительности стоянки, мин"
    )


class PreviewOut(BaseModel):
    minutes: int
    amount: MoneyOut
    billing_minutes: int
    applied_rules: list[str]


class BreakdownOut(BaseModel):
    kind: str
    starts_at: datetime
    ends_at: datetime
    minutes: int
    amount: MoneyOut
    note: str


class QuoteOut(BaseModel):
    amount: MoneyOut
    gross: MoneyOut
    discount: MoneyOut
    billing_minutes: int
    applied_rules: list[str]
    tariff_plan_version_id: str | None
    breakdown: list[BreakdownOut]

    @classmethod
    def of(cls, c: TariffCalculation) -> QuoteOut:
        return cls(
            amount=MoneyOut(**c.amount.to_dict()),
            gross=MoneyOut(**c.gross.to_dict()),
            discount=MoneyOut(**c.discount.to_dict()),
            billing_minutes=c.billing_minutes,
            applied_rules=list(c.applied_rules),
            tariff_plan_version_id=str(c.tariff_plan_version_id) if c.tariff_plan_version_id else None,
            breakdown=[
                BreakdownOut(
                    kind=b.kind,
                    starts_at=b.starts_at,
                    ends_at=b.ends_at,
                    minutes=b.minutes,
                    amount=MoneyOut(**b.amount.to_dict()),
                    note=b.note,
                )
                for b in c.breakdown
            ],
        )


@router.get(
    "/lots/{lot_id}/tariff-plans", response_model=list[PlanOut], openapi_extra=permission_extra(Permission.TARIFF_READ)
)
async def list_plans(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.TARIFF_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[PlanOut]:
    return [PlanOut.of(p) for p in await container.tariffs.list_plans(user, lot_id)]


@router.post(
    "/lots/{lot_id}/tariff-plans",
    response_model=PlanOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.TARIFF_WRITE),
)
async def create_plan(
    lot_id: UUID,
    body: CreatePlanRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.TARIFF_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> PlanOut:
    return PlanOut.of(
        await container.tariffs.create_plan(
            user, lot_id, code=body.code, name=body.name, is_default=body.is_default, reason=body.reason
        )
    )


@router.get(
    "/tariff-plans/{plan_id}/versions",
    response_model=list[VersionOut],
    openapi_extra=permission_extra(Permission.TARIFF_READ),
)
async def list_versions(
    plan_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.TARIFF_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[VersionOut]:
    return [VersionOut.of(v) for v in await container.tariffs.list_versions(user, plan_id)]


@router.post(
    "/tariff-plans/{plan_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.TARIFF_WRITE),
)
async def publish_version(
    plan_id: UUID,
    body: PublishVersionRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.TARIFF_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> VersionOut:
    cmd = PublishVersionCommand(
        plan_id=plan_id,
        rules=body.rules,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        holiday_calendar_id=body.holiday_calendar_id,
    )
    return VersionOut.of(await container.tariffs.publish_version(user, cmd, reason=body.reason))


@router.post("/tariff/quote", response_model=QuoteOut, openapi_extra=permission_extra(Permission.TARIFF_READ))
async def quote(
    body: QuoteRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.TARIFF_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> QuoteOut:
    calc = await container.tariffs.quote(
        user,
        body.lot_id,
        body.entry,
        body.exit,
        AccessTerms(kind=body.access_kind, discount_percent=body.discount_percent),
    )
    return QuoteOut.of(calc)


@router.post(
    "/tariff/preview", response_model=list[PreviewOut], openapi_extra=permission_extra(Permission.TARIFF_WRITE)
)
async def preview(
    body: PreviewRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.TARIFF_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> list[PreviewOut]:
    """Пример расчёта по черновику тарифа до публикации — экран «Тариф» показывает, сколько заплатят."""
    start = body.start or container.clock.now()
    calcs = await container.tariffs.preview(
        user, body.lot_id, body.rules, start=start, durations=body.durations_minutes
    )
    return [
        PreviewOut(
            minutes=minutes,
            amount=MoneyOut(**c.amount.to_dict()),
            billing_minutes=c.billing_minutes,
            applied_rules=list(c.applied_rules),
        )
        for minutes, c in zip(body.durations_minutes, calcs, strict=True)
    ]
