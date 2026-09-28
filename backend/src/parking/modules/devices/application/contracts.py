"""Контракт Devices: для Parking — запросить открытие шлагбаума в текущей транзакции (исполнение — вне неё);
для ANPR/Media/API — принципал камеры, инфо шлагбаума и его кеш, heartbeat, реестр драйверов, представления."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from parking.modules.devices.application.camera_auth import CameraPrincipal
from parking.modules.devices.application.ports import (
    BarrierDriver,
    BarrierInfo,
    BarrierInfoCache,
    DriverRegistry,
    DriverResult,
    Heartbeat,
    HeartbeatStore,
)
from parking.modules.devices.domain.barrier import Barrier, BarrierDriverKind, BarrierState
from parking.modules.devices.domain.camera import Camera, CameraApiKey, CameraDirection, CameraStatus, CameraVendor
from parking.modules.devices.domain.command import CommandKind, CommandSource, CommandState

__all__ = [
    "Barrier",
    "BarrierCommandResult",
    "BarrierCommands",
    "BarrierDriver",
    "BarrierDriverKind",
    "BarrierInfo",
    "BarrierInfoCache",
    "BarrierState",
    "Camera",
    "CameraApiKey",
    "CameraDirection",
    "CameraPrincipal",
    "CameraStatus",
    "CameraVendor",
    "CommandKind",
    "CommandSource",
    "CommandState",
    "DriverRegistry",
    "DriverResult",
    "Heartbeat",
    "HeartbeatStore",
]


@dataclass(frozen=True, slots=True)
class BarrierCommandResult:
    command_id: UUID | None  # None — у камеры/площадки нет шлагбаума: открывать нечего
    barrier_id: UUID | None
    state: CommandState


class BarrierCommands(Protocol):
    async def request_open(
        self,
        *,
        lot_id: UUID,
        camera_id: UUID | None,
        session_id: UUID | None,
        saga_id: UUID | None,
        source: CommandSource,
        idempotency_key: bytes,
        issued_by: UUID | None = None,
        reason: str | None = None,
        fresh_key: bool = False,
    ) -> BarrierCommandResult:
        """Пишет команду PENDING и публикует devices.barrier.command_requested.v1 в той же UoW.
        Повтор с тем же idempotency_key возвращает существующую команду; `fresh_key=True` — вызывающий гарантирует,
        что ключ новый (агрегат создан в этой транзакции), и проверка реестра пропускается."""
        ...

    async def status(self, command_id: UUID) -> CommandState | None: ...
