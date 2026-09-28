"""Логин / refresh / logout / me. Refresh — только в httpOnly-cookie (фаза 6 §5), access — в теле."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from pydantic import BaseModel, Field, model_validator

from parking.api.deps import PUBLIC, client_info, current_user, get_container
from parking.modules.identity.application.auth_service import ClientInfo
from parking.modules.identity.application.contracts import CurrentUser, TokenPair
from parking.platform.config import Environment
from parking.runtime.container import Container
from parking.shared.errors import AuthenticationError

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
REFRESH_COOKIE = "parking_refresh"
COOKIE_PATH = "/api/v1/auth"


class LoginRequest(BaseModel):
    """v2 этап 4: `login` — e-mail или телефон; `email` оставлен для старых клиентов."""

    login: str | None = Field(default=None, min_length=3, max_length=254)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _one_login(self) -> LoginRequest:
        if not (self.login or self.email):
            raise ValueError("login or email is required")
        return self

    @property
    def identifier(self) -> str:
        return self.login or self.email or ""


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 — тип токена, не секрет
    expires_in: int


class MeResponse(BaseModel):
    user_id: str
    tenant_id: str
    role: str
    permissions: list[str]
    lot_scope: list[str]
    display_name: str | None = None  # v2: шапка панели — имя сотрудника и название клиента
    email: str | None = None
    tenant_name: str | None = None
    phone: str | None = None  # v2 этап 4
    must_change_password: bool = False  # временный пароль: клиент показывает экран смены пароля
    # v2 этап 3: подписка клиента — баннер пробного периода / блокировки настроек
    plan_code: str | None = None
    tenant_status: str | None = None
    trial_ends_at: datetime | None = None
    trial_days_left: int | None = None
    paid_until: datetime | None = None
    blocked_reason: str | None = None


def _set_refresh_cookie(response: Response, container: Container, pair: TokenPair) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        pair.refresh_token,
        max_age=pair.refresh_expires_in,
        httponly=True,
        secure=container.settings.env is not Environment.DEV,
        samesite="strict",
        path=COOKIE_PATH,
    )


@router.post("/login", response_model=TokenResponse, openapi_extra=PUBLIC)
async def login(
    body: LoginRequest,
    response: Response,
    container: Annotated[Container, Depends(get_container)],
    client: Annotated[ClientInfo, Depends(client_info)],
) -> TokenResponse:
    pair = await container.auth.login(body.identifier, body.password, client)
    _set_refresh_cookie(response, container, pair)
    return TokenResponse(access_token=pair.access_token, expires_in=pair.access_expires_in)


@router.post("/refresh", response_model=TokenResponse, openapi_extra=PUBLIC)
async def refresh(
    response: Response,
    container: Annotated[Container, Depends(get_container)],
    client: Annotated[ClientInfo, Depends(client_info)],
    parking_refresh: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    if not parking_refresh:
        raise AuthenticationError("missing refresh token")
    pair = await container.auth.refresh(parking_refresh, client)
    _set_refresh_cookie(response, container, pair)
    return TokenResponse(access_token=pair.access_token, expires_in=pair.access_expires_in)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, openapi_extra=PUBLIC)
async def logout(
    response: Response,
    container: Annotated[Container, Depends(get_container)],
    client: Annotated[ClientInfo, Depends(client_info)],
    parking_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    if parking_refresh:
        await container.auth.logout(parking_refresh, client)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)


@router.get("/me", response_model=MeResponse, openapi_extra={"x-permission": []})
async def me(
    user: Annotated[CurrentUser, Depends(current_user)],
    container: Annotated[Container, Depends(get_container)],
) -> MeResponse:
    async with container.uow(user.tenant_id) as uow:
        profile = await container.users.get(user, user.user_id)
        tenant = await container.tenant_reader(uow).current(user.tenant_id)
    sub = await container.subscriptions.brief(user.tenant_id)
    return MeResponse(
        user_id=str(user.user_id),
        tenant_id=str(user.tenant_id),
        role=user.role.value,
        permissions=sorted(p.value for p in user.permissions),
        lot_scope=sorted(str(lot) for lot in user.lot_scope),
        display_name=profile.display_name if profile is not None else None,
        email=profile.email if profile is not None else None,
        phone=profile.phone if profile is not None else None,
        must_change_password=user.must_change_password,
        tenant_name=tenant.name if tenant is not None else None,
        plan_code=sub.plan_code if sub else None,
        tenant_status=sub.status if sub else None,
        trial_ends_at=sub.trial_ends_at if sub else None,
        trial_days_left=sub.trial_days_left if sub else None,
        paid_until=sub.paid_until if sub else None,
        blocked_reason=sub.blocked_reason if sub else None,
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT, openapi_extra={"x-permission": []})
async def change_password(
    body: ChangePasswordRequest,
    user: Annotated[CurrentUser, Depends(current_user)],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    """Смена своего пароля — в том числе обязательная после временного. Все сессии завершаются: клиент входит заново."""
    await container.users.change_own_password(
        user, current_password=body.current_password, new_password=body.new_password
    )
