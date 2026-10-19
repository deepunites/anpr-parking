from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.pagination import MAX_LIMIT, Page, decode_cursor, encode_cursor
from parking.api.schemas import Reason
from parking.modules.access.application.admin_service import AddRuleCommand, CreateSubscriptionCommand
from parking.modules.access.application.contracts import AccessKind, AccessRuleView
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.parking.application.contracts import VehicleAttributesView
from parking.runtime.container import Container
from parking.shared.errors import NotFoundError
from parking.shared.plate import PlateNumber

router = APIRouter(prefix="/api/v1", tags=["access"])


class RuleOut(BaseModel):
    id: str
    lot_id: str | None
    kind: AccessKind
    plate: str
    valid_from: datetime
    valid_to: datetime | None
    discount_percent: int
    source_id: str | None
    reason: str | None
    revoked_at: datetime | None
    created_at: datetime
    vehicle_make: str | None = None  # v2: марка/модель/цвет из карточки машины
    vehicle_model: str | None = None
    vehicle_color: str | None = None

    @classmethod
    def of(cls, r: AccessRuleView, vehicle: VehicleAttributesView | None = None) -> RuleOut:
        return cls(
            id=str(r.id),
            lot_id=str(r.lot_id) if r.lot_id else None,
            kind=r.kind,
            plate=r.plate,
            valid_from=r.valid_from,
            valid_to=r.valid_to,
            discount_percent=r.discount_percent,
            source_id=str(r.source_id) if r.source_id else None,
            reason=r.reason,
            revoked_at=r.revoked_at,
            created_at=r.created_at,
            vehicle_make=vehicle.make if vehicle else None,
            vehicle_model=vehicle.model if vehicle else None,
            vehicle_color=vehicle.color if vehicle else None,
        )


class AddRuleRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    kind: AccessKind
    lot_id: UUID | None = None
    discount_percent: int = Field(default=0, ge=0, le=100)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    note: str | None = Field(default=None, max_length=200)
    make: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    reason: Reason


class CreateSubscriptionRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    lot_id: UUID | None = None
    plan_code: str = Field(min_length=1, max_length=30)
    starts_at: datetime
    ends_at: datetime
    price_minor: int = Field(ge=0)
    make: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    reason: Reason


class ReasonRequest(BaseModel):
    reason: Reason


class DecisionOut(BaseModel):
    kind: AccessKind
    discount_percent: int
    source_id: str | None
    rule_id: str | None
    opens_barrier_automatically: bool
    plate: str  # маскированный


@router.get("/access-rules", response_model=Page[RuleOut], openapi_extra=permission_extra(Permission.ACCESS_READ))
async def list_rules(
    user: Annotated[CurrentUser, Depends(require(Permission.ACCESS_READ))],
    container: Annotated[Container, Depends(get_container)],
    kind: AccessKind | None = None,
    lot_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> Page[RuleOut]:
    rules = await container.access.list_rules(
        user, kind=kind, lot_id=lot_id, limit=limit + 1, after=decode_cursor(cursor)
    )
    page = rules[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rules) > limit and page else None
    attrs = await container.parking.vehicle_attributes_for(user, _plates(page))
    return Page(items=[RuleOut.of(r, attrs.get(r.plate)) for r in page], next_cursor=next_cursor)


@router.post(
    "/access-rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.ACCESS_WRITE),
)
async def add_rule(
    body: AddRuleRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.ACCESS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> RuleOut:
    cmd = AddRuleCommand(
        plate=body.plate,
        kind=body.kind,
        lot_id=body.lot_id,
        discount_percent=body.discount_percent,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        note=body.note,
    )
    rule = await container.access.add_rule(
        user, cmd, reason=body.reason
    )  # сначала правило: отказ — без побочных записей
    vehicle = await _save_vehicle(container, user, body.plate, body.make, body.model, body.color, body.reason)
    return RuleOut.of(rule, vehicle)


@router.post(
    "/access-rules/{rule_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.ACCESS_WRITE),
)
async def revoke_rule(
    rule_id: UUID,
    body: ReasonRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.ACCESS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.access.revoke_rule(user, rule_id, reason=body.reason)


@router.post(
    "/subscriptions",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PASSES_SELL),
)
async def create_subscription(
    body: CreateSubscriptionRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PASSES_SELL))],
    container: Annotated[Container, Depends(get_container)],
) -> RuleOut:
    cmd = CreateSubscriptionCommand(
        plate=body.plate,
        lot_id=body.lot_id,
        plan_code=body.plan_code,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        price_minor=body.price_minor,
    )
    rule = await container.access.create_subscription(user, cmd, reason=body.reason)
    vehicle = await _save_vehicle(container, user, body.plate, body.make, body.model, body.color, body.reason)
    return RuleOut.of(rule, vehicle)


class SubscriptionReceiptRequest(BaseModel):
    lot_id: UUID = Field(description="площадка, где продан абонемент (номер чека — по площадке)")


class SubscriptionReceiptOut(BaseModel):
    printed: bool
    receipt_no: str
    amount_minor: int


@router.post(
    "/subscriptions/{subscription_id}/receipt",
    response_model=SubscriptionReceiptOut,
    openapi_extra=permission_extra(Permission.PASSES_SELL),
)
async def print_subscription_receipt(
    subscription_id: UUID,
    body: SubscriptionReceiptRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PASSES_SELL))],
    container: Annotated[Container, Depends(get_container)],
) -> SubscriptionReceiptOut:
    """Чек месячного абонемента: номер машины крупно, срок, сумма, сквозной номер чека (повторная печать — тот же)."""
    receipt_no, amount = await container.receipts.print_subscription(user, subscription_id, body.lot_id)
    return SubscriptionReceiptOut(printed=True, receipt_no=receipt_no, amount_minor=amount.minor)


@router.get("/access/resolve", response_model=DecisionOut, openapi_extra=permission_extra(Permission.ACCESS_READ))
async def resolve(
    lot_id: UUID,
    plate: Annotated[str, Query(min_length=4, max_length=20)],
    user: Annotated[CurrentUser, Depends(require(Permission.ACCESS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> DecisionOut:
    """Что решит система для номера на площадке прямо сейчас — для оператора и поддержки."""
    allowed = user.allowed_lots()
    if allowed is not None and lot_id not in allowed:
        raise NotFoundError("lot not found")
    number = PlateNumber.parse(plate)
    decision = await container.access_resolver.resolve(
        user.tenant_id, lot_id, container.plate_crypto.hash(user.tenant_id, number), container.clock.now()
    )
    return DecisionOut(
        kind=decision.kind,
        discount_percent=decision.discount_percent,
        source_id=str(decision.source_id) if decision.source_id else None,
        rule_id=str(decision.rule_id) if decision.rule_id else None,
        opens_barrier_automatically=decision.opens_barrier_automatically,
        plate=number.masked,
    )


def _plates(rules: list[AccessRuleView]) -> list[PlateNumber]:
    out = []
    for r in rules:
        try:
            out.append(PlateNumber.parse(r.plate))
        except ValueError:  # номер не расшифровался / анонимизирован — марки у него нет
            continue
    return out


async def _save_vehicle(
    container: Container,
    user: CurrentUser,
    plate: str,
    make: str | None,
    model: str | None,
    color: str | None,
    reason: str,
) -> VehicleAttributesView | None:
    """Марка из формы «добавить машину» пишется в карточку машины (модуль parking) до правила доступа."""
    if not (make or model or color):
        return None
    view = await container.parking.set_vehicle_attributes(
        user, PlateNumber.parse(plate), make=make, model=model, color=color, reason=reason
    )
    return view.attributes
