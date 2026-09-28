"""v2 этап 4: приглашения сотрудников. Администратор: /api/v1/invites. Сотрудник по ссылке: /api/v1/join/{token}
(публично — токен 256 бит, в БД только sha256; ответ не раскрывает ничего, кроме имени клиента и роли)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel, Field

from parking.api.deps import PUBLIC, get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission, Role
from parking.modules.identity.application.invite_service import InviteCommand, InviteView
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1", tags=["invites"])
Token = Annotated[str, Path(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")]


class InviteOut(BaseModel):
    id: str
    display_name: str
    role: Role
    email: str | None
    phone: str | None
    lot_scope: list[str]
    status: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None

    @classmethod
    def of(cls, v: InviteView) -> InviteOut:
        return cls(
            id=str(v.id),
            display_name=v.display_name,
            role=v.role,
            email=v.email,
            phone=v.phone,
            lot_scope=sorted(str(lot) for lot in v.lot_scope),
            status=v.status.value,
            created_at=v.created_at,
            expires_at=v.expires_at,
            accepted_at=v.accepted_at,
        )


class CreateInviteRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: Role
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    lot_scope: list[UUID] = Field(default_factory=list)
    reason: Reason


class IssuedInviteOut(BaseModel):
    invite: InviteOut
    token: str = Field(description="показывается один раз: ссылка для сотрудника — /join/{token}")


class RevokeInviteRequest(BaseModel):
    reason: Reason


class JoinPreviewOut(BaseModel):
    tenant_name: str
    display_name: str
    role: Role
    login: str
    expires_at: datetime


class AcceptInviteRequest(BaseModel):
    password: str = Field(min_length=12, max_length=512)
    display_name: str | None = Field(default=None, max_length=100)
    locale: str = Field(default="ru", pattern="^(ru|uz)$")


class AcceptedOut(BaseModel):
    login: str


@router.get("/invites", response_model=list[InviteOut], openapi_extra=permission_extra(Permission.USERS_READ))
async def list_invites(
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[InviteOut]:
    return [InviteOut.of(v) for v in await container.invites.list(user)]


@router.post(
    "/invites",
    response_model=IssuedInviteOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.USERS_WRITE),
)
async def create_invite(
    body: CreateInviteRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> IssuedInviteOut:
    issued = await container.invites.create(
        user,
        InviteCommand(
            display_name=body.display_name,
            role=body.role,
            email=body.email,
            phone=body.phone,
            lot_scope=frozenset(body.lot_scope),
        ),
        reason=body.reason,
    )
    return IssuedInviteOut(invite=InviteOut.of(issued.invite), token=issued.token)


@router.post(
    "/invites/{invite_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=permission_extra(Permission.USERS_WRITE),
)
async def revoke_invite(
    invite_id: UUID,
    body: RevokeInviteRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.USERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> None:
    await container.invites.revoke(user, invite_id, reason=body.reason)


@router.get("/join/{token}", response_model=JoinPreviewOut, openapi_extra=PUBLIC)
async def join_preview(token: Token, container: Annotated[Container, Depends(get_container)]) -> JoinPreviewOut:
    preview = await container.invites.preview(token)
    async with container.uow(preview.tenant_id) as uow:
        tenant = await container.tenant_reader(uow).current(preview.tenant_id)
    return JoinPreviewOut(
        tenant_name=tenant.name if tenant is not None else "",
        display_name=preview.display_name,
        role=preview.role,
        login=preview.login,
        expires_at=preview.expires_at,
    )


@router.post("/join/{token}", response_model=AcceptedOut, openapi_extra=PUBLIC)
async def join_accept(
    token: Token, body: AcceptInviteRequest, container: Annotated[Container, Depends(get_container)]
) -> AcceptedOut:
    accepted = await container.invites.accept(
        token, password=body.password, display_name=body.display_name, locale=body.locale
    )
    return AcceptedOut(login=accepted.login)
