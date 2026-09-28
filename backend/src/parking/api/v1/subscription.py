"""v2 этап 3 «Продажа»: тарифы платформы, самостоятельная регистрация клиента, подписка, клиенты владельца платформы."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, Field

from parking.api.deps import PUBLIC, client_info, get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.identity.application.auth_service import ClientInfo
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.tenancy.application.contracts import Plan, TenantStatus
from parking.runtime.container import Container
from parking.shared.errors import ValidationError

router = APIRouter(prefix="/api/v1", tags=["subscription"])


class PlatformPlanOut(BaseModel):
    code: str
    name: str
    max_lots: int | None
    max_places: int | None
    max_cameras: int | None
    price_minor: int | None
    trial_days: int

    @classmethod
    def of(cls, p: Plan) -> PlatformPlanOut:
        return cls(
            code=p.code,
            name=p.name,
            max_lots=p.max_lots,
            max_places=p.max_places,
            max_cameras=p.max_cameras,
            price_minor=p.price_minor,
            trial_days=p.trial_days,
        )


class SignupRequest(BaseModel):
    company: str = Field(min_length=2, max_length=150)
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    password: str = Field(min_length=12, max_length=512)
    plan_code: str = Field(min_length=2, max_length=20)
    locale: str = Field(default="ru", pattern="^(ru|uz)$")
    accept_terms: bool


class SignupStartedOut(BaseModel):
    signup_id: str
    channel: str
    expires_at: datetime
    dev_code: str | None = None


class SignupVerifyRequest(BaseModel):
    code: str = Field(min_length=4, max_length=10)


class SignupCompletedOut(BaseModel):
    tenant_id: str
    email: str


class UsageOut(BaseModel):
    lots: int
    places: int
    cameras: int


class SubscriptionOut(BaseModel):
    plan: PlatformPlanOut
    status: str
    trial_ends_at: datetime | None
    paid_until: datetime | None
    trial_days_left: int | None
    blocked_reason: str | None
    usage: UsageOut
    plans: list[PlatformPlanOut]


class ChangePlanRequest(BaseModel):
    plan_code: str = Field(min_length=2, max_length=20)
    reason: Reason


class PlatformTenantOut(BaseModel):
    id: str
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


class PlatformTenantUpdateRequest(BaseModel):
    status: TenantStatus | None = None
    plan_code: str | None = Field(default=None, max_length=20)
    paid_until: datetime | None = None
    trial_ends_at: datetime | None = None
    reason: Reason


@router.get("/plans", response_model=list[PlatformPlanOut], openapi_extra=PUBLIC)
async def list_plans(container: Annotated[Container, Depends(get_container)]) -> list[PlatformPlanOut]:
    """Тарифы для страницы регистрации — публично, без данных клиентов."""
    return [PlatformPlanOut.of(p) for p in await container.subscriptions.plans()]


@router.post("/signup", response_model=SignupStartedOut, status_code=status.HTTP_201_CREATED, openapi_extra=PUBLIC)
async def start_signup(
    body: SignupRequest,
    container: Annotated[Container, Depends(get_container)],
    client: Annotated[ClientInfo, Depends(client_info)],
) -> SignupStartedOut:
    if not body.accept_terms:
        raise ValidationError("terms must be accepted", code="TERMS_NOT_ACCEPTED")
    started = await container.signup.start(
        company=body.company,
        name=body.name,
        email=body.email,
        phone=body.phone,
        password=body.password,
        plan_code=body.plan_code,
        locale=body.locale,
        ip=client.ip,
    )
    return SignupStartedOut(
        signup_id=started.signup_id, channel=started.channel, expires_at=started.expires_at, dev_code=started.dev_code
    )


@router.post("/signup/{signup_id}/verify", response_model=SignupCompletedOut, openapi_extra=PUBLIC)
async def verify_signup(
    signup_id: Annotated[str, Path(min_length=10, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")],
    body: SignupVerifyRequest,
    container: Annotated[Container, Depends(get_container)],
) -> SignupCompletedOut:
    done = await container.signup.verify(signup_id, body.code)
    return SignupCompletedOut(tenant_id=str(done.tenant_id), email=done.email)


@router.get(
    "/subscription", response_model=SubscriptionOut, openapi_extra=permission_extra(Permission.SUBSCRIPTION_MANAGE)
)
async def get_subscription(
    user: Annotated[CurrentUser, Depends(require(Permission.SUBSCRIPTION_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> SubscriptionOut:
    view = await container.subscriptions.view(user, cameras=await container.devices.active_camera_count(user.tenant_id))
    sub = view.subscription
    return SubscriptionOut(
        plan=PlatformPlanOut.of(sub.plan),
        status=sub.status.value,
        trial_ends_at=sub.trial_ends_at,
        paid_until=sub.paid_until,
        trial_days_left=view.trial_days_left,
        blocked_reason=view.blocked_reason,
        usage=UsageOut(lots=view.usage.lots, places=view.usage.places, cameras=view.usage.cameras),
        plans=[PlatformPlanOut.of(p) for p in view.plans],
    )


@router.post(
    "/subscription/plan",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.SUBSCRIPTION_MANAGE),
)
async def change_plan(
    body: ChangePlanRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SUBSCRIPTION_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    cameras = await container.devices.active_camera_count(user.tenant_id)
    await container.subscriptions.change_plan(user, body.plan_code, cameras=cameras, reason=body.reason)


@router.get(
    "/platform/tenants",
    response_model=list[PlatformTenantOut],
    openapi_extra=permission_extra(Permission.PLATFORM_VIEW),
)
async def platform_tenants(
    _user: Annotated[CurrentUser, Depends(require(Permission.PLATFORM_VIEW))],
    container: Annotated[Container, Depends(get_container)],
) -> list[PlatformTenantOut]:
    """Клиенты платформы — агрегаты (площадки, места, камеры, сотрудники, сессии за 30 дней), без данных водителей."""
    return [
        PlatformTenantOut(
            id=str(r.id),
            code=r.code,
            name=r.name,
            plan_code=r.plan_code,
            status=r.status,
            trial_ends_at=r.trial_ends_at,
            paid_until=r.paid_until,
            created_at=r.created_at,
            contact_phone=r.contact_phone,
            lots=r.lots,
            places=r.places,
            cameras=r.cameras,
            users=r.users,
            sessions_30d=r.sessions_30d,
        )
        for r in await container.platform.list_tenants()
    ]


@router.patch(
    "/platform/tenants/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.TENANTS_MANAGE),
)
async def platform_update_tenant(
    tenant_id: UUID,
    body: PlatformTenantUpdateRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.TENANTS_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    """Владелец платформы: активировать после оплаты (paid_until), продлить пробный период, сменить тариф, выключить."""
    await container.platform.update_tenant(
        user,
        tenant_id,
        status=body.status,
        plan_code=body.plan_code,
        paid_until=body.paid_until,
        trial_ends_at=body.trial_ends_at,
        reason=body.reason,
    )
