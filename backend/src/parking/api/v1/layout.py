"""v2 этап 5 «Карта»: схема площадки (версии) и живая карта — занятость по зонам и состояние устройств.

Карта собирается здесь из трёх модулей: схема (tenancy), занятость по камерам въезда (parking), камеры и стрелы
(devices). Зона машины — зона камеры, через которую она въехала; ручной въезд и камеры без зоны — «вне зон»."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.tenancy.application.layout_service import LayoutView
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1/lots/{lot_id}", tags=["layout"])


class ZoneOut(BaseModel):
    id: str
    name: str
    color: str
    capacity: int
    spots: int


class LayoutOut(BaseModel):
    lot_id: str
    version: int = Field(description="0 — схемы ещё нет")
    document: dict[str, Any]
    spots: int
    lot_capacity: int
    zones: list[ZoneOut]
    updated_at: datetime | None
    updated_by: str | None

    @classmethod
    def of(cls, v: LayoutView) -> LayoutOut:
        layout = v.parsed()
        return cls(
            lot_id=str(v.lot_id),
            version=v.version,
            document=v.document,
            spots=len(layout.spots),
            lot_capacity=v.capacity,
            zones=[
                ZoneOut(
                    id=z.id,
                    name=z.name,
                    color=z.color,
                    capacity=layout.zone_capacity(z),
                    spots=sum(1 for s in layout.spots if s.zone_id == z.id),
                )
                for z in layout.zones
            ],
            updated_at=v.updated_at,
            updated_by=str(v.updated_by) if v.updated_by else None,
        )


class SaveLayoutRequest(BaseModel):
    base_version: int = Field(ge=0, description="версия, которую открыл редактор; устарела — 409 LAYOUT_CONFLICT")
    document: dict[str, Any]
    reason: str | None = Field(default=None, max_length=500)


class LayoutVersionOut(BaseModel):
    version: int
    spots: int
    created_at: datetime
    created_by: str | None
    reason: str | None


class ZoneLoadOut(BaseModel):
    id: str
    name: str
    color: str
    capacity: int
    occupied: int


class MapDeviceOut(BaseModel):
    element_id: str
    kind: str
    ref_id: str
    name: str
    status: str
    zone_id: str | None


class MapOut(BaseModel):
    lot_id: str
    version: int
    document: dict[str, Any]
    capacity: int
    occupied: int
    zones: list[ZoneLoadOut]
    unzoned_occupied: int = Field(description="ручные въезды и камеры без зоны на схеме")
    devices: list[MapDeviceOut]


async def _known_devices(container: Container, user: CurrentUser, lot_id: UUID) -> set[tuple[str, UUID]]:
    cameras = await container.devices.list_cameras(user)
    barriers = await container.devices.list_barriers(user)
    return {("camera", c.id) for c in cameras if c.lot_id == lot_id} | {
        ("barrier", b.id) for b in barriers if b.lot_id == lot_id
    }


@router.get("/layout", response_model=LayoutOut, openapi_extra=permission_extra(Permission.LOTS_READ))
async def get_layout(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> LayoutOut:
    return LayoutOut.of(await container.layouts.get(user, lot_id))


@router.put("/layout", response_model=LayoutOut, openapi_extra=permission_extra(Permission.LOTS_WRITE))
async def save_layout(
    lot_id: UUID,
    body: SaveLayoutRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> LayoutOut:
    """Новая версия схемы; места рядов синхронизируются в справочник мест (код, зона, координаты)."""
    saved = await container.layouts.save(
        user,
        lot_id,
        base_version=body.base_version,
        document=body.document,
        known_devices=await _known_devices(container, user, lot_id),
        reason=body.reason,
    )
    return LayoutOut.of(saved)


@router.get(
    "/layout/versions", response_model=list[LayoutVersionOut], openapi_extra=permission_extra(Permission.LOTS_READ)
)
async def layout_versions(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[LayoutVersionOut]:
    return [
        LayoutVersionOut(
            version=r.version,
            spots=r.spots,
            created_at=r.created_at,
            created_by=str(r.created_by) if r.created_by else None,
            reason=r.reason,
        )
        for r in await container.layouts.history(user, lot_id)
    ]


@router.get(
    "/layout/versions/{version}", response_model=LayoutOut, openapi_extra=permission_extra(Permission.LOTS_READ)
)
async def layout_version(
    lot_id: UUID,
    version: Annotated[int, Path(ge=1)],
    user: Annotated[CurrentUser, Depends(require(Permission.LOTS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> LayoutOut:
    """Старая версия — для просмотра и отката (откат = сохранение её документа новой версией)."""
    return LayoutOut.of(await container.layouts.get_version(user, lot_id, version))


@router.get("/map", response_model=MapOut, openapi_extra=permission_extra(Permission.SESSIONS_READ))
async def live_map(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> MapOut:
    view = await container.layouts.get(user, lot_id)
    layout = view.parsed()
    by_camera = await container.parking.occupancy_by_entry_camera(user, lot_id)
    camera_zone = layout.camera_zones()
    per_zone: dict[str, int] = {}
    unzoned = 0
    for camera_id, count in by_camera.items():
        zone_id = camera_zone.get(camera_id) if camera_id is not None else None
        if zone_id is None:
            unzoned += count
        else:
            per_zone[zone_id] = per_zone.get(zone_id, 0) + count
    cameras = {c.id: c for c in await container.devices.list_cameras(user) if c.lot_id == lot_id}
    barriers = {b.id: b for b in await container.devices.list_barriers(user) if b.lot_id == lot_id}
    devices: list[MapDeviceOut] = []
    for d in layout.devices:
        if d.kind == "camera" and d.ref_id in cameras:
            c = cameras[d.ref_id]
            status = c.status.value if c.active else "DISABLED"
            devices.append(
                MapDeviceOut(
                    element_id=d.id,
                    kind="camera",
                    ref_id=str(c.id),
                    name=c.name,
                    status=status,
                    zone_id=camera_zone.get(c.id),
                )
            )
        elif d.kind == "barrier" and d.ref_id in barriers:
            b = barriers[d.ref_id]
            status = b.state.value if b.active else "DISABLED"
            devices.append(
                MapDeviceOut(
                    element_id=d.id, kind="barrier", ref_id=str(b.id), name=b.name, status=status, zone_id=d.zone_id
                )
            )
    return MapOut(
        lot_id=str(lot_id),
        version=view.version,
        document=view.document,
        capacity=view.capacity,
        occupied=sum(by_camera.values()),
        zones=[
            ZoneLoadOut(
                id=z.id, name=z.name, color=z.color, capacity=layout.zone_capacity(z), occupied=per_zone.get(z.id, 0)
            )
            for z in layout.zones
        ],
        unzoned_occupied=unzoned,
        devices=devices,
    )
