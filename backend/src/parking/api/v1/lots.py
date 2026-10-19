from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.tenancy.application.contracts import LotPolicies, LotView, UnknownExitPolicy
from parking.modules.tenancy.application.lot_service import CreateLotCommand, UpdateLotCommand
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1/lots", tags=["lots"])


class PoliciesModel(BaseModel):
    max_session_hours: int = Field(default=72, ge=1, le=24 * 365)
    reentry_window_minutes: int = Field(default=5, ge=0, le=60)
    unknown_exit_policy: UnknownExitPolicy = UnknownExitPolicy.MANUAL_REVIEW
    deny_entry_when_full: bool = False
    exit_grace_minutes: int = Field(default=15, ge=0, le=240)
    exit_inferred_after_minutes: int = Field(default=60, ge=1, le=1440)
    late_event_window_hours: int = Field(default=24, ge=1, le=168)
    barrier_cidr_allowlist: list[str] = Field(default_factory=list)
    show_full_plates: bool = False  # сотрудникам — номер целиком, а не маской 01A***AA
    payment_qr: str | None = Field(
        default=None, max_length=512, description="строка статичного QR банка (UzQR) — печатается на «Счёте к оплате»"
    )
    exit_min_stay_seconds: int = Field(
        default=0,
        ge=0,
        le=600,
        description="выезд раньше — эхо въезда (камера выезда видит въезжающую машину), 0 — выкл.",
    )

    def to_domain(self) -> LotPolicies:
        return LotPolicies(
            max_session_hours=self.max_session_hours,
            reentry_window_minutes=self.reentry_window_minutes,
            unknown_exit_policy=self.unknown_exit_policy,
            deny_entry_when_full=self.deny_entry_when_full,
            exit_grace_minutes=self.exit_grace_minutes,
            exit_inferred_after_minutes=self.exit_inferred_after_minutes,
            late_event_window_hours=self.late_event_window_hours,
            barrier_cidr_allowlist=tuple(self.barrier_cidr_allowlist),
            show_full_plates=self.show_full_plates,
            exit_min_stay_seconds=self.exit_min_stay_seconds,
            payment_qr=(self.payment_qr or "").strip() or None,
        )

    @classmethod
    def of(cls, p: LotPolicies) -> PoliciesModel:
        return cls(
            max_session_hours=p.max_session_hours,
            reentry_window_minutes=p.reentry_window_minutes,
            unknown_exit_policy=p.unknown_exit_policy,
            deny_entry_when_full=p.deny_entry_when_full,
            exit_grace_minutes=p.exit_grace_minutes,
            exit_inferred_after_minutes=p.exit_inferred_after_minutes,
            late_event_window_hours=p.late_event_window_hours,
            barrier_cidr_allowlist=list(p.barrier_cidr_allowlist),
            show_full_plates=p.show_full_plates,
            exit_min_stay_seconds=p.exit_min_stay_seconds,
            payment_qr=p.payment_qr,
        )


class LotOut(BaseModel):
    id: str
    code: str
    name: str
    address: str | None
    capacity: int
    timezone: str
    active: bool
    policies: PoliciesModel

    @classmethod
    def of(cls, lot: LotView) -> LotOut:
        return cls(
            id=str(lot.id),
            code=lot.code,
            name=lot.name,
            address=lot.address,
            capacity=lot.capacity,
            timezone=lot.timezone,
            active=lot.active,
            policies=PoliciesModel.of(lot.policies),
        )


class CreateLotRequest(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    capacity: int = Field(gt=0, le=100_000)
    timezone: str = Field(default="Asia/Tashkent", max_length=50)
    address: str | None = None
    policies: PoliciesModel = Field(default_factory=PoliciesModel)
    reason: Reason


class UpdateLotRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    capacity: int | None = Field(default=None, gt=0, le=100_000)
    address: str | None = None
    policies: PoliciesModel | None = None
    reason: Reason


@router.get("", response_model=list[LotOut], openapi_extra=permission_extra(Permission.LOTS_READ))
async def list_lots(
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[LotOut]:
    return [LotOut.of(lot) for lot in await container.lots.list(user)]


@router.get("/{lot_id}", response_model=LotOut, openapi_extra=permission_extra(Permission.LOTS_READ))
async def get_lot(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> LotOut:
    return LotOut.of(await container.lots.get(user, lot_id))


@router.post(
    "",
    response_model=LotOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.LOTS_WRITE),
)
async def create_lot(
    body: CreateLotRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> LotOut:
    lot = await container.lots.create(
        user,
        CreateLotCommand(
            code=body.code,
            name=body.name,
            capacity=body.capacity,
            timezone=body.timezone,
            address=body.address,
            policies=body.policies.to_domain(),
        ),
        reason=body.reason,
    )
    return LotOut.of(lot)


@router.patch("/{lot_id}", response_model=LotOut, openapi_extra=permission_extra(Permission.LOTS_WRITE))
async def update_lot(
    lot_id: UUID,
    body: UpdateLotRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> LotOut:
    lot = await container.lots.update(
        user,
        lot_id,
        UpdateLotCommand(
            name=body.name,
            capacity=body.capacity,
            address=body.address,
            policies=body.policies.to_domain() if body.policies else None,
        ),
        reason=body.reason,
    )
    return LotOut.of(lot)
