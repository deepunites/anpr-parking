"""v2: карточка машины (марка, модель, цвет) и ручная регистрация въезда оператором.

Номер передаётся только в теле запроса, не в URL: адреса попадают в журналы доступа прокси (I-10)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.api.v1.sessions import SessionOut
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.parking.application.contracts import ManualEntryCommand, VehicleView
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber

router = APIRouter(prefix="/api/v1", tags=["vehicles"])


class VehicleOut(BaseModel):
    plate: str  # маскированный
    plate_format: str
    known: bool
    make: str | None
    model: str | None
    color: str | None
    active_session_id: str | None

    @classmethod
    def of(cls, v: VehicleView) -> VehicleOut:
        return cls(
            plate=v.plate,
            plate_format=v.plate_format,
            known=v.known,
            make=v.attributes.make,
            model=v.attributes.model,
            color=v.attributes.color,
            active_session_id=str(v.active_session_id) if v.active_session_id else None,
        )


class VehicleLookupRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)


class VehicleAttributesRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    make: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    reason: Reason


class SessionVehicleRequest(BaseModel):
    make: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    reason: Reason


class ManualEntryRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    entry_time: datetime | None = None
    make: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    reason: Reason


@router.post("/vehicles/lookup", response_model=VehicleOut, openapi_extra=permission_extra(Permission.SESSIONS_READ))
async def lookup_vehicle(
    body: VehicleLookupRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> VehicleOut:
    """Что система знает о машине: марка/модель/цвет и стоит ли она сейчас на доступной площадке."""
    return VehicleOut.of(await container.parking.vehicle_info(user, PlateNumber.parse(body.plate)))


@router.post("/vehicles", response_model=VehicleOut, openapi_extra=permission_extra(Permission.SESSIONS_WRITE))
async def set_vehicle_attributes(
    body: VehicleAttributesRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> VehicleOut:
    return VehicleOut.of(
        await container.parking.set_vehicle_attributes(
            user,
            PlateNumber.parse(body.plate),
            make=body.make,
            model=body.model,
            color=body.color,
            reason=body.reason,
        )
    )


@router.post(
    "/lots/{lot_id}/manual-entry",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE),
)
async def manual_entry(
    lot_id: UUID,
    body: ManualEntryRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    """Ручной въезд: камера не распознала машину, оператор регистрирует её вручную (шлагбаум не открывается)."""
    cmd = ManualEntryCommand(
        lot_id=lot_id,
        plate=body.plate,
        entry_time=body.entry_time,
        make=body.make,
        model=body.model,
        color=body.color,
    )
    return SessionOut.of(await container.parking.register_manual_entry(user, cmd, reason=body.reason))


@router.post(
    "/sessions/{session_id}/vehicle",
    response_model=SessionOut,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE),
)
async def set_session_vehicle(
    session_id: UUID,
    body: SessionVehicleRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    """Марка/модель/цвет машины этой сессии — из карточки, без ввода номера."""
    return SessionOut.of(
        await container.parking.set_session_vehicle_attributes(
            user, session_id, make=body.make, model=body.model, color=body.color, reason=body.reason
        )
    )
