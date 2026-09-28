from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from parking.api.deps import get_container, permission_extra, require
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.runtime.container import Container
from parking.shared.errors import NotFoundError

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


class TenantOut(BaseModel):
    id: str
    code: str
    name: str
    currency: str
    timezone: str
    active: bool


@router.get("/current", response_model=TenantOut, openapi_extra=permission_extra(Permission.LOTS_READ))
async def current_tenant(
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> TenantOut:
    async with container.uow(user.tenant_id) as uow:
        info = await container.tenant_reader(uow).current(user.tenant_id)
    if info is None:
        raise NotFoundError("tenant not found")
    return TenantOut(
        id=str(info.id),
        code=info.code,
        name=info.name,
        currency=info.currency,
        timezone=info.timezone,
        active=info.active,
    )
