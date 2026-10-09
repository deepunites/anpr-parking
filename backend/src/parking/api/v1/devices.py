"""Администрирование устройств: камеры, ключи, шлагбаумы; ручное открытие — только с reason (I-15)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.devices.application.admin import (
    CreateBarrierCommand,
    CreateCameraCommand,
    UpdateBarrierCommand,
    UpdateCameraCommand,
)
from parking.modules.devices.application.contracts import (
    Barrier,
    BarrierDriverKind,
    Camera,
    CameraApiKey,
    CameraDirection,
    CameraVendor,
)
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1", tags=["devices"])


class CameraOut(BaseModel):
    id: str
    lot_id: str
    code: str
    name: str
    direction: CameraDirection
    vendor: CameraVendor
    location: str | None
    confidence_min: int
    confidence_review_min: int
    max_clock_skew_seconds: int
    heartbeat_interval_sec: int
    active: bool
    status: str
    last_heartbeat_at: datetime | None
    last_event_at: datetime | None

    @classmethod
    def of(cls, c: Camera) -> CameraOut:
        return cls(
            id=str(c.id),
            lot_id=str(c.lot_id),
            code=c.code,
            name=c.name,
            direction=c.direction,
            vendor=c.vendor,
            location=c.location,
            confidence_min=c.confidence_min,
            confidence_review_min=c.confidence_review_min,
            max_clock_skew_seconds=c.max_clock_skew_seconds,
            heartbeat_interval_sec=c.heartbeat_interval_sec,
            active=c.active,
            status=c.status.value,
            last_heartbeat_at=c.last_heartbeat_at,
            last_event_at=c.last_event_at,
        )


class CreateCameraRequest(BaseModel):
    lot_id: UUID
    code: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]{1,30}$")
    name: str = Field(min_length=1, max_length=100)
    direction: CameraDirection
    vendor: CameraVendor = CameraVendor.GENERIC
    location: str | None = None
    confidence_min: int = Field(default=80, ge=0, le=100)
    confidence_review_min: int = Field(default=60, ge=0, le=100)
    max_clock_skew_seconds: int = Field(default=120, gt=0, le=86_400)
    heartbeat_interval_sec: int = Field(default=300, gt=0, le=86_400)
    reason: Reason


class UpdateCameraRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    location: str | None = None
    confidence_min: int | None = Field(default=None, ge=0, le=100)
    confidence_review_min: int | None = Field(default=None, ge=0, le=100)
    max_clock_skew_seconds: int | None = Field(default=None, gt=0, le=86_400)
    heartbeat_interval_sec: int | None = Field(default=None, gt=0, le=86_400)
    active: bool | None = None
    reason: Reason


class KeyOut(BaseModel):
    id: str
    camera_id: str
    key_prefix: str
    label: str | None
    active: bool
    created_at: datetime | None
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def of(cls, k: CameraApiKey) -> KeyOut:
        return cls(
            id=str(k.id),
            camera_id=str(k.camera_id),
            key_prefix=k.key_prefix,
            label=k.label,
            active=k.active,
            created_at=k.created_at,
            expires_at=k.expires_at,
            last_used_at=k.last_used_at,
            revoked_at=k.revoked_at,
        )


class IssuedKeyOut(KeyOut):
    key: str  # показывается один раз


class IssueKeyRequest(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    reason: Reason


class RotateKeyRequest(BaseModel):
    grace_hours: int = Field(default=24, ge=0, le=24 * 30)
    reason: Reason


class ReasonBody(BaseModel):
    reason: Reason


class BarrierOut(BaseModel):
    id: str
    lot_id: str
    camera_id: str | None
    name: str
    driver: BarrierDriverKind
    endpoint: str
    auth_secret_ref: str | None
    auto_close_seconds: int | None
    active: bool
    state: str
    has_credentials: bool = Field(default=False, description="логин/пароль контроллера заданы (сами не возвращаются)")

    @classmethod
    def of(cls, b: Barrier) -> BarrierOut:
        return cls(
            id=str(b.id),
            lot_id=str(b.lot_id),
            camera_id=str(b.camera_id) if b.camera_id else None,
            name=b.name,
            driver=b.driver,
            endpoint=b.endpoint,
            auth_secret_ref=b.auth_secret_ref,
            auto_close_seconds=b.auto_close_seconds,
            active=b.active,
            state=b.state.value,
            has_credentials=b.credentials_encrypted is not None,
        )


class CreateBarrierRequest(BaseModel):
    lot_id: UUID
    name: str = Field(min_length=1, max_length=100)
    driver: BarrierDriverKind
    endpoint: str = Field(min_length=1, max_length=255)
    camera_id: UUID | None = None
    auth_secret_ref: str | None = Field(default=None, max_length=255)
    auto_close_seconds: int | None = Field(default=None, gt=0, le=600)
    username: str | None = Field(default=None, max_length=64, description="HIKVISION_ISAPI: логин камеры")
    password: str | None = Field(
        default=None, max_length=128, description="HIKVISION_ISAPI: пароль камеры (не возвращается)"
    )
    reason: Reason


class UpdateBarrierRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    endpoint: str | None = Field(default=None, min_length=1, max_length=255)
    camera_id: UUID | None = None
    auth_secret_ref: str | None = Field(default=None, max_length=255)
    auto_close_seconds: int | None = Field(default=None, gt=0, le=600)
    active: bool | None = None
    username: str | None = Field(default=None, max_length=64, description="новые логин и пароль — только парой")
    password: str | None = Field(default=None, max_length=128)
    reason: Reason


class CommandOut(BaseModel):
    command_id: str | None
    barrier_id: str | None
    state: str


# ---------- камеры ----------


@router.get("/cameras", response_model=list[CameraOut], openapi_extra=permission_extra(Permission.CAMERAS_READ))
async def list_cameras(
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[CameraOut]:
    return [CameraOut.of(c) for c in await container.devices.list_cameras(user)]


@router.get("/cameras/{camera_id}", response_model=CameraOut, openapi_extra=permission_extra(Permission.CAMERAS_READ))
async def get_camera(
    camera_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> CameraOut:
    return CameraOut.of(await container.devices.get_camera(user, camera_id))


@router.post(
    "/cameras",
    response_model=CameraOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.CAMERAS_WRITE),
)
async def create_camera(
    body: CreateCameraRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> CameraOut:
    camera = await container.devices.create_camera(
        user,
        CreateCameraCommand(
            lot_id=body.lot_id,
            code=body.code,
            name=body.name,
            direction=body.direction,
            vendor=body.vendor,
            location=body.location,
            confidence_min=body.confidence_min,
            confidence_review_min=body.confidence_review_min,
            max_clock_skew_seconds=body.max_clock_skew_seconds,
            heartbeat_interval_sec=body.heartbeat_interval_sec,
        ),
        reason=body.reason,
    )
    return CameraOut.of(camera)


@router.patch(
    "/cameras/{camera_id}", response_model=CameraOut, openapi_extra=permission_extra(Permission.CAMERAS_WRITE)
)
async def update_camera(
    camera_id: UUID,
    body: UpdateCameraRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> CameraOut:
    camera = await container.devices.update_camera(
        user,
        camera_id,
        UpdateCameraCommand(
            name=body.name,
            location=body.location,
            confidence_min=body.confidence_min,
            confidence_review_min=body.confidence_review_min,
            max_clock_skew_seconds=body.max_clock_skew_seconds,
            heartbeat_interval_sec=body.heartbeat_interval_sec,
            active=body.active,
        ),
        reason=body.reason,
    )
    return CameraOut.of(camera)


# ---------- ключи ----------


@router.get(
    "/cameras/{camera_id}/keys",
    response_model=list[KeyOut],
    openapi_extra=permission_extra(Permission.CAMERAS_ROTATE_KEY),
)
async def list_keys(
    camera_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_ROTATE_KEY))],
    container: Annotated[Container, Depends(get_container)],
) -> list[KeyOut]:
    return [KeyOut.of(k) for k in await container.devices.list_keys(user, camera_id)]


@router.post(
    "/cameras/{camera_id}/keys",
    response_model=IssuedKeyOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.CAMERAS_ROTATE_KEY),
)
async def issue_key(
    camera_id: UUID,
    body: IssueKeyRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_ROTATE_KEY))],
    container: Annotated[Container, Depends(get_container)],
) -> IssuedKeyOut:
    issued = await container.devices.issue_key(
        user,
        camera_id,
        label=body.label,
        expires_in=timedelta(days=body.expires_in_days) if body.expires_in_days else None,
        reason=body.reason,
    )
    return IssuedKeyOut(**KeyOut.of(issued.key).model_dump(), key=issued.raw)


@router.post(
    "/camera-keys/{key_id}/rotate",
    response_model=IssuedKeyOut,
    openapi_extra=permission_extra(Permission.CAMERAS_ROTATE_KEY),
)
async def rotate_key(
    key_id: UUID,
    body: RotateKeyRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_ROTATE_KEY))],
    container: Annotated[Container, Depends(get_container)],
) -> IssuedKeyOut:
    issued = await container.devices.rotate_key(
        user, key_id, grace=timedelta(hours=body.grace_hours), reason=body.reason
    )
    return IssuedKeyOut(**KeyOut.of(issued.key).model_dump(), key=issued.raw)


@router.post(
    "/camera-keys/{key_id}/revoke", response_model=KeyOut, openapi_extra=permission_extra(Permission.CAMERAS_ROTATE_KEY)
)
async def revoke_key(
    key_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(Permission.CAMERAS_ROTATE_KEY))],
    container: Annotated[Container, Depends(get_container)],
) -> KeyOut:
    return KeyOut.of(await container.devices.revoke_key(user, key_id, reason=body.reason))


# ---------- шлагбаумы ----------


@router.get("/barriers", response_model=list[BarrierOut], openapi_extra=permission_extra(Permission.BARRIERS_READ))
async def list_barriers(
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIERS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[BarrierOut]:
    return [BarrierOut.of(b) for b in await container.devices.list_barriers(user)]


@router.get(
    "/barriers/{barrier_id}", response_model=BarrierOut, openapi_extra=permission_extra(Permission.BARRIERS_READ)
)
async def get_barrier(
    barrier_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIERS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> BarrierOut:
    return BarrierOut.of(await container.devices.get_barrier(user, barrier_id))


@router.post(
    "/barriers",
    response_model=BarrierOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.BARRIERS_WRITE),
)
async def create_barrier(
    body: CreateBarrierRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> BarrierOut:
    barrier = await container.devices.create_barrier(
        user,
        CreateBarrierCommand(
            lot_id=body.lot_id,
            name=body.name,
            driver=body.driver,
            endpoint=body.endpoint,
            camera_id=body.camera_id,
            auth_secret_ref=body.auth_secret_ref,
            auto_close_seconds=body.auto_close_seconds,
            username=body.username,
            password=body.password,
        ),
        reason=body.reason,
    )
    return BarrierOut.of(barrier)


@router.patch(
    "/barriers/{barrier_id}", response_model=BarrierOut, openapi_extra=permission_extra(Permission.BARRIERS_WRITE)
)
async def update_barrier(
    barrier_id: UUID,
    body: UpdateBarrierRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> BarrierOut:
    barrier = await container.devices.update_barrier(
        user,
        barrier_id,
        UpdateBarrierCommand(
            name=body.name,
            endpoint=body.endpoint,
            camera_id=body.camera_id,
            auth_secret_ref=body.auth_secret_ref,
            auto_close_seconds=body.auto_close_seconds,
            active=body.active,
            username=body.username,
            password=body.password,
        ),
        reason=body.reason,
    )
    return BarrierOut.of(barrier)


@router.post(
    "/barriers/{barrier_id}/open",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra=permission_extra(Permission.BARRIER_OPEN),
)
async def open_barrier(
    barrier_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIER_OPEN))],
    container: Annotated[Container, Depends(get_container)],
) -> CommandOut:
    """Ручное открытие (I-15): reason обязателен (иначе 422); команда и аудит пишутся до отправки, исполняет воркер."""
    result = await container.devices.open_barrier(user, barrier_id, reason=body.reason)
    return CommandOut(
        command_id=str(result.command_id) if result.command_id else None,
        barrier_id=str(result.barrier_id) if result.barrier_id else None,
        state=result.state.value,
    )


class ProbeOut(BaseModel):
    ok: bool
    latency_ms: int | None
    error: str | None
    detail: str | None


@router.post(
    "/barriers/{barrier_id}/probe",
    response_model=ProbeOut,
    openapi_extra=permission_extra(Permission.BARRIERS_WRITE),
)
async def probe_barrier(
    barrier_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIERS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> ProbeOut:
    """v2: проверка связи с контроллером шлагбаума без движения стрелы (Modbus — чтение реле, HTTP — TCP)."""
    r = await container.devices.probe_barrier(user, barrier_id, reason=body.reason)
    return ProbeOut(ok=r.ok, latency_ms=r.latency_ms, error=r.error, detail=r.detail)


class CommandStatusOut(BaseModel):
    command_id: str
    state: str
    attempts: int
    latency_ms: int | None
    error: str | None


@router.get(
    "/barrier-commands/{command_id}",
    response_model=CommandStatusOut,
    openapi_extra=permission_extra(Permission.BARRIER_OPEN),
)
async def barrier_command_status(
    command_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIER_OPEN))],
    container: Annotated[Container, Depends(get_container)],
) -> CommandStatusOut:
    """v2: состояние команды шлагбаума — PENDING/SENT → OK | FAILED | SKIPPED (исполняет воркер)."""
    c = await container.devices.command(user, command_id)
    return CommandStatusOut(
        command_id=str(c.id), state=c.state.value, attempts=c.attempts, latency_ms=c.latency_ms, error=c.last_error
    )
