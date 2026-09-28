from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.pagination import MAX_LIMIT, Page, decode_cursor, encode_cursor
from parking.api.schemas import Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission, Role, UserView
from parking.modules.identity.application.user_service import CreateUserCommand
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1/users", tags=["users"])


class UserOut(BaseModel):
    id: str
    email: str | None
    phone: str | None = None
    role: Role
    display_name: str
    locale: str
    active: bool
    lot_scope: list[str]
    must_change_password: bool = False  # ещё не сменил временный пароль
    last_login_at: datetime | None = None

    @classmethod
    def of(cls, user: UserView) -> UserOut:
        return cls(
            id=str(user.id),
            email=user.email,
            phone=user.phone,
            role=user.role,
            display_name=user.display_name,
            locale=user.locale,
            active=user.active,
            lot_scope=sorted(str(lot) for lot in user.lot_scope),
            must_change_password=user.must_change_password,
            last_login_at=user.last_login_at,
        )


class CreateUserRequest(BaseModel):
    """Сотрудник с временным паролем от администратора (сменит при первом входе). Логин — e-mail и/или телефон."""

    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    password: str = Field(min_length=12, max_length=512)
    role: Role
    display_name: str = Field(min_length=1, max_length=100)
    locale: str = Field(default="ru", pattern="^(ru|uz)$")
    lot_scope: list[UUID] = Field(default_factory=list)
    reason: Reason


class ChangeRoleRequest(BaseModel):
    role: Role
    reason: Reason


class ReasonRequest(BaseModel):
    reason: Reason


@router.get("", response_model=Page[UserOut], openapi_extra=permission_extra(Permission.USERS_READ))
async def list_users(
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_READ))],
    container: Annotated[Container, Depends(get_container)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> Page[UserOut]:
    after = decode_cursor(cursor)
    users = await container.users.list(user, limit=limit + 1, after=after)
    page = users[:limit]
    next_cursor = None
    if len(users) > limit and page and page[-1].created_at is not None:
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id)
    return Page(items=[UserOut.of(u) for u in page], next_cursor=next_cursor)


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.USERS_WRITE),
)
async def create_user(
    body: CreateUserRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> UserOut:
    created = await container.users.create(
        user,
        CreateUserCommand(
            email=body.email,
            phone=body.phone,
            temporary_password=True,
            password=body.password,
            role=body.role,
            display_name=body.display_name,
            locale=body.locale,
            lot_scope=frozenset(body.lot_scope),
        ),
        reason=body.reason,
    )
    return UserOut.of(created)


@router.get("/{user_id}", response_model=UserOut, openapi_extra=permission_extra(Permission.USERS_READ))
async def get_user(
    user_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> UserOut:
    return UserOut.of(await container.users.get(user, user_id))


@router.patch("/{user_id}/role", response_model=UserOut, openapi_extra=permission_extra(Permission.USERS_WRITE))
async def change_role(
    user_id: UUID,
    body: ChangeRoleRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> UserOut:
    return UserOut.of(await container.users.change_role(user, user_id, body.role, reason=body.reason))


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=254, description="пустая строка — убрать e-mail")
    phone: str | None = Field(default=None, max_length=30, description="пустая строка — убрать телефон")
    locale: str | None = Field(default=None, pattern="^(ru|uz)$")
    lot_scope: list[UUID] | None = None
    active: bool | None = None
    reason: str = Field(min_length=3, max_length=500)


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=512)
    reason: str = Field(min_length=3, max_length=500)


@router.patch("/{user_id}", response_model=UserOut, openapi_extra=permission_extra(Permission.USERS_WRITE))
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> UserOut:
    return UserOut.of(
        await container.users.update(
            user,
            user_id,
            display_name=body.display_name,
            locale=body.locale,
            lot_scope=frozenset(body.lot_scope) if body.lot_scope is not None else None,
            active=body.active,
            reason=body.reason,
            email=body.email,
            phone=body.phone,
        )
    )


@router.post(
    "/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.USERS_WRITE),
)
async def reset_password(
    user_id: UUID,
    body: ResetPasswordRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.users.reset_password(user, user_id, password=body.password, reason=body.reason)


@router.post(
    "/{user_id}/deactivate",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.USERS_WRITE),
)
async def deactivate_user(
    user_id: UUID,
    body: ReasonRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.users.deactivate(user, user_id, reason=body.reason)
