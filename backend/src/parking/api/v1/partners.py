"""Партнёры площадки (MediaPark, 2026-11-06: компьютерный клуб выпускает своих гостей).

/partner/* — бармен клуба (роль PARTNER, право partner:validate): находит машину гостя по номеру и выпускает её за
счёт клуба, видит свои отметки за месяц. /partners/* — администратор площадки (partners:manage): партнёры, цена за
машину, входы барменов и расчёт с партнёром за месяц."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission, Role
from parking.modules.identity.application.user_service import CreateUserCommand
from parking.modules.parking.application.partners import PartnerCar, PartnerMonth, ValidationView
from parking.modules.parking.application.ports import Partner, PartnerMember
from parking.runtime.container import Container
from parking.shared.errors import ValidationError

router = APIRouter(prefix="/api/v1", tags=["partners"])
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
TASHKENT = ZoneInfo("Asia/Tashkent")  # текущий месяц по умолчанию; границы месяца — по часам площадки (сервис)


class PartnerOut(BaseModel):
    id: str
    lot_id: str
    name: str
    monthly_fee_minor: int = Field(description="фиксированная плата партнёра в месяц, тийины")
    price_per_car_minor: int = Field(description="доплата за каждую отмеченную машину (обычно 0), тийины")
    active: bool

    @classmethod
    def of(cls, p: Partner) -> PartnerOut:
        return cls(
            id=str(p.id),
            lot_id=str(p.lot_id),
            name=p.name,
            monthly_fee_minor=p.monthly_fee,
            price_per_car_minor=p.price_per_car,
            active=p.active,
        )


class PartnerCarOut(BaseModel):
    session_id: str
    plate: str
    entry_time: datetime
    validated: bool = Field(description="уже отмечена партнёром")
    exact: bool = Field(description="номер совпал точно; иначе — похожий (камера ошиблась в букве или регионе)")

    @classmethod
    def of(cls, c: PartnerCar) -> PartnerCarOut:
        return cls(
            session_id=str(c.session_id), plate=c.plate, entry_time=c.entry_time, validated=c.validated, exact=c.exact
        )


class ValidationOut(BaseModel):
    id: str
    session_id: str
    plate: str | None
    entry_time: datetime
    exit_time: datetime | None
    state: str = Field(description="PARKED — на площадке, LEFT — выехала, CANCELLED — отметка отменена")
    price_per_car_minor: int
    created_at: datetime
    created_by: str | None
    cancel_reason: str | None

    @classmethod
    def of(cls, v: ValidationView) -> ValidationOut:
        return cls(
            id=str(v.id),
            session_id=str(v.session_id),
            plate=v.plate,
            entry_time=v.entry_time,
            exit_time=v.exit_time,
            state=v.state,
            price_per_car_minor=v.price_per_car,
            created_at=v.created_at,
            created_by=v.created_by,
            cancel_reason=v.cancel_reason,
        )


class PartnerMonthOut(BaseModel):
    partner: PartnerOut
    month: str
    count: int = Field(description="машин за месяц, без отменённых отметок")
    cars_total_minor: int = Field(description="за машины (если задана цена за машину)")
    total_minor: int = Field(description="к оплате партнёром за месяц: плата в месяц + за машины")
    items: list[ValidationOut]

    @classmethod
    def of(cls, m: PartnerMonth) -> PartnerMonthOut:
        return cls(
            partner=PartnerOut.of(m.partner),
            month=m.month.strftime("%Y-%m"),
            count=m.count,
            cars_total_minor=m.cars_total,
            total_minor=m.total,
            items=[ValidationOut.of(v) for v in m.items],
        )


class MemberOut(BaseModel):
    user_id: str
    display_name: str
    login: str | None
    active: bool

    @classmethod
    def of(cls, m: PartnerMember) -> MemberOut:
        return cls(user_id=str(m.user_id), display_name=m.display_name, login=m.login, active=m.active)


class ValidateRequest(BaseModel):
    session_id: UUID


class ValidatedOut(BaseModel):
    validation_id: str


class CancelRequest(BaseModel):
    reason: Reason


class CreatePartnerRequest(BaseModel):
    lot_id: UUID
    name: str = Field(min_length=1, max_length=100)
    monthly_fee_minor: int = Field(default=0, ge=0, description="сколько партнёр платит площадке в месяц, тийины")
    price_per_car_minor: int = Field(default=0, ge=0, description="доплата за каждую машину (обычно 0), тийины")
    reason: Reason


class UpdatePartnerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    monthly_fee_minor: int | None = Field(default=None, ge=0)
    price_per_car_minor: int | None = Field(default=None, ge=0)
    active: bool | None = None
    reason: Reason


class CreateMemberRequest(BaseModel):
    """Вход бармена партнёра: временный пароль от администратора (сменит при первом входе)."""

    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=512)
    reason: Reason


def _month(raw: str | None) -> date:
    if raw is None:
        return datetime.now(tz=TASHKENT).date().replace(day=1)
    year, month = raw.split("-")
    return date(int(year), int(month), 1)


# ---------------------------------------------------------------- бармен партнёра
@router.get("/partner/me", response_model=PartnerOut, openapi_extra=permission_extra(Permission.PARTNER_VALIDATE))
async def my_partner(
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNER_VALIDATE))],
    container: Annotated[Container, Depends(get_container)],
) -> PartnerOut:
    return PartnerOut.of(await container.partners.my_partner(user))


@router.get(
    "/partner/cars", response_model=list[PartnerCarOut], openapi_extra=permission_extra(Permission.PARTNER_VALIDATE)
)
async def find_cars(
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNER_VALIDATE))],
    container: Annotated[Container, Depends(get_container)],
    plate: Annotated[str, Query(min_length=4, max_length=20)],
) -> list[PartnerCarOut]:
    """Машина гостя на площадке по номеру (или очень похожая — камера ошиблась), не больше трёх."""
    return [PartnerCarOut.of(c) for c in await container.partners.find_cars(user, plate)]


@router.post(
    "/partner/validations",
    response_model=ValidatedOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PARTNER_VALIDATE),
)
async def validate(
    body: ValidateRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNER_VALIDATE))],
    container: Annotated[Container, Depends(get_container)],
) -> ValidatedOut:
    """«Выпустить за счёт клуба»: на выезде к оплате 0 сум и стрела открывается сама."""
    return ValidatedOut(validation_id=str(await container.partners.validate(user, body.session_id)))


@router.get(
    "/partner/validations",
    response_model=PartnerMonthOut,
    openapi_extra=permission_extra(Permission.PARTNER_VALIDATE),
)
async def my_validations(
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNER_VALIDATE))],
    container: Annotated[Container, Depends(get_container)],
    month: Annotated[str | None, Query(pattern=MONTH, description="ГГГГ-ММ; по умолчанию — текущий")] = None,
) -> PartnerMonthOut:
    return PartnerMonthOut.of(await container.partners.my_month(user, _month(month)))


@router.post(
    "/partner/validations/{validation_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.PARTNER_VALIDATE),
)
async def cancel_my_validation(
    validation_id: UUID,
    body: CancelRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNER_VALIDATE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    """Отмена отметки (ошиблись машиной), пока машина не выехала."""
    await container.partners.cancel(user, validation_id, reason=body.reason)


# ---------------------------------------------------------------- администратор площадки
@router.get("/partners", response_model=list[PartnerOut], openapi_extra=permission_extra(Permission.PARTNERS_MANAGE))
async def list_partners(
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> list[PartnerOut]:
    return [PartnerOut.of(p) for p in await container.partners.list_partners(user)]


@router.post(
    "/partners",
    response_model=PartnerOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PARTNERS_MANAGE),
)
async def create_partner(
    body: CreatePartnerRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> PartnerOut:
    partner = await container.partners.create_partner(
        user,
        lot_id=body.lot_id,
        name=body.name,
        price_per_car=body.price_per_car_minor,
        monthly_fee=body.monthly_fee_minor,
        reason=body.reason,
    )
    return PartnerOut.of(partner)


@router.patch(
    "/partners/{partner_id}", response_model=PartnerOut, openapi_extra=permission_extra(Permission.PARTNERS_MANAGE)
)
async def update_partner(
    partner_id: UUID,
    body: UpdatePartnerRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> PartnerOut:
    partner = await container.partners.update_partner(
        user,
        partner_id,
        name=body.name,
        price_per_car=body.price_per_car_minor,
        monthly_fee=body.monthly_fee_minor,
        active=body.active,
        reason=body.reason,
    )
    return PartnerOut.of(partner)


@router.get(
    "/partners/{partner_id}/members",
    response_model=list[MemberOut],
    openapi_extra=permission_extra(Permission.PARTNERS_MANAGE),
)
async def list_members(
    partner_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> list[MemberOut]:
    return [MemberOut.of(m) for m in await container.partners.members(user, partner_id)]


@router.post(
    "/partners/{partner_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PARTNERS_MANAGE, Permission.USERS_WRITE),
)
async def create_member(
    partner_id: UUID,
    body: CreateMemberRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE, Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> MemberOut:
    """Вход бармена: пользователь с ролью PARTNER только на площадке партнёра — видит лишь поиск машины гостя."""
    partner = await container.partners.get_partner(user, partner_id)  # 404 чужому — раньше проверки тела
    if not (body.email or body.phone):
        raise ValidationError("email or phone is required", code="LOGIN_REQUIRED")
    created = await container.users.create(
        user,
        CreateUserCommand(
            email=body.email,
            phone=body.phone,
            temporary_password=True,
            password=body.password,
            role=Role.PARTNER,
            display_name=body.display_name,
            locale="ru",
            lot_scope=frozenset({partner.lot_id}),
        ),
        reason=body.reason,
    )
    await container.partners.add_member(user, partner.id, created.id)
    return MemberOut(
        user_id=str(created.id), display_name=created.display_name, login=body.email or body.phone, active=True
    )


@router.get(
    "/partners/{partner_id}/report",
    response_model=PartnerMonthOut,
    openapi_extra=permission_extra(Permission.PARTNERS_MANAGE),
)
async def partner_report(
    partner_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
    month: Annotated[str | None, Query(pattern=MONTH, description="ГГГГ-ММ; по умолчанию — текущий")] = None,
) -> PartnerMonthOut:
    """Расчёт с партнёром за месяц: сколько машин отмечено и сколько он должен площадке."""
    return PartnerMonthOut.of(await container.partners.report(user, partner_id, _month(month)))


@router.post(
    "/partners/{partner_id}/validations/{validation_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.PARTNERS_MANAGE),
)
async def cancel_validation(
    partner_id: UUID,
    validation_id: UUID,
    body: CancelRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PARTNERS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.partners.get_partner(user, partner_id)
    await container.partners.cancel(user, validation_id, reason=body.reason)
