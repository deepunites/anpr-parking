from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from parking.modules.devices.domain.barrier import Barrier
from parking.modules.devices.domain.camera import Camera, CameraApiKey, CameraStatus
from parking.modules.devices.domain.command import BarrierCommand
from parking.modules.devices.domain.endpoint import IPAddress
from parking.platform.db.uow import UnitOfWork
from parking.platform.secrets import SecretProvider as SecretProvider  # noqa: PLC0414 — часть публичного порта


@dataclass(frozen=True, slots=True)
class BarrierInfo:
    id: UUID
    lot_id: UUID
    driver: str
    endpoint: str
    auth_secret_ref: str | None
    active: bool
    camera_id: UUID | None = None
    name: str = ""
    allowlist: tuple[str, ...] = ()  # parking_lot.barrier_cidr_allowlist — нужен драйверу для SSRF-проверки
    tenant_id: UUID | None = None  # для расшифровки credentials_encrypted (AAD — клиент)
    credentials_encrypted: bytes | None = None  # HIKVISION_ISAPI: логин/пароль камеры (расшифровывает драйвер)


class BarrierRepository(Protocol):
    async def for_camera(self, lot_id: UUID, camera_id: UUID | None) -> BarrierInfo | None:
        """Шлагбаум камеры; без камеры — первый активный шлагбаум площадки."""
        ...

    async def get(self, barrier_id: UUID) -> BarrierInfo | None: ...

    async def get_entity(self, barrier_id: UUID, *, for_update: bool = False) -> Barrier | None: ...
    async def list(self, *, lot_ids: frozenset[UUID] | None) -> list[Barrier]: ...
    async def add(self, barrier: Barrier) -> None: ...
    async def save(self, barrier: Barrier) -> None: ...


class BarrierInfoCache(Protocol):
    """Шлагбаум камеры в Redis — для оффлайн-открытия, когда PostgreSQL недоступен (A-10)."""

    async def remember(self, barrier: BarrierInfo) -> None: ...
    async def for_camera(self, lot_id: UUID, camera_id: UUID) -> BarrierInfo | None: ...


class BarrierCommandRepository(Protocol):
    async def add(self, command: BarrierCommand) -> None: ...
    async def find_by_key(self, idempotency_key: bytes) -> BarrierCommand | None: ...
    async def get(self, command_id: UUID) -> BarrierCommand | None: ...
    async def get_for_update(self, command_id: UUID) -> BarrierCommand | None: ...
    async def save(self, command: BarrierCommand) -> None: ...


@dataclass(frozen=True, slots=True)
class DriverResult:
    ok: bool
    error: str | None = None
    latency_ms: int | None = None
    detail: str | None = None  # v2: итог проверки связи (состояние реле и т. п.)


class BarrierDriver(Protocol):
    async def open(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult:
        """Исходящий вызов контроллера — только вне транзакции (I-9)."""
        ...

    async def close(self, barrier: BarrierInfo, command_id: UUID) -> DriverResult: ...

    async def probe(self, barrier: BarrierInfo) -> DriverResult:
        """v2: проверка связи без движения стрелы (Modbus — чтение катушек, HTTP — TCP-подключение)."""
        ...


class DriverRegistry(Protocol):
    def driver_for(self, barrier: BarrierInfo) -> BarrierDriver:
        """Бросает ServiceUnavailableError, если драйверы выключены или тип неизвестен — не тихий no-op (CORE §7)."""
        ...


class CameraRepository(Protocol):
    async def get(self, camera_id: UUID, *, for_update: bool = False) -> Camera | None: ...
    async def list(self, *, lot_ids: frozenset[UUID] | None) -> list[Camera]: ...
    async def add(self, camera: Camera) -> None: ...
    async def save(self, camera: Camera) -> None: ...


class CameraKeyRepository(Protocol):
    async def add(self, key: CameraApiKey) -> None: ...
    async def get(self, key_id: UUID, *, for_update: bool = False) -> CameraApiKey | None: ...
    async def list_for_camera(self, camera_id: UUID) -> list[CameraApiKey]: ...
    async def save(self, key: CameraApiKey) -> None: ...


@dataclass(frozen=True, slots=True)
class KeyCandidate:
    """Строка app.lookup_camera_keys: всё, что нужно ingest'у, одним запросом до установки tenant-контекста."""

    key_id: UUID
    tenant_id: UUID
    camera_id: UUID
    lot_id: UUID
    key_hmac: bytes
    pepper_version: int
    expires_at: datetime | None
    camera_active: bool
    direction: str
    code: str
    confidence_min: int
    confidence_review_min: int
    max_clock_skew_seconds: int
    late_event_window_hours: int
    tenant_active: bool


class CameraKeyLookup(Protocol):
    async def candidates(self, key_prefix: str) -> list[KeyCandidate]:
        """SECURITY DEFINER-функция: единственный pre-tenant запрос аутентификации камеры."""
        ...


class CameraAuthCache(Protocol):
    async def get(self, key_prefix: str) -> list[KeyCandidate] | None:
        """None — промах; пустой список — закешированный отрицательный результат."""
        ...

    async def get_stale(self, key_prefix: str) -> list[KeyCandidate] | None:
        """Долгоживущая копия (сутки) — только когда PostgreSQL недоступен: камеры продолжают работать в оффлайне."""
        ...

    async def set(self, key_prefix: str, candidates: Sequence[KeyCandidate], ttl_seconds: int) -> None: ...
    async def invalidate(self, key_prefix: str) -> None: ...
    async def touch(self, key_id: UUID, at: datetime) -> None:
        """last_used_at — в Redis, в PostgreSQL сбрасывается батчем (CORE §3)."""
        ...

    async def count_failure(self, subject: str, window_seconds: int) -> int: ...


@dataclass(frozen=True, slots=True)
class Heartbeat:
    camera_id: UUID
    at: datetime
    firmware_version: str | None = None


class HeartbeatStore(Protocol):
    async def record(self, hb: Heartbeat, ttl_seconds: int) -> None: ...
    async def read_many(self, camera_ids: Sequence[UUID]) -> dict[UUID, Heartbeat]: ...
    async def note_event(self, camera_id: UUID, at: datetime) -> None:
        """Обновляет last_event и базовую линию межсобытийного интервала по часу суток (для SILENT)."""
        ...

    async def expected_gap_seconds(self, camera_id: UUID, hour: int) -> float | None: ...
    async def last_events(self, camera_ids: Sequence[UUID]) -> dict[UUID, datetime]: ...
    async def drain_key_usage(self) -> dict[UUID, datetime]: ...


@dataclass(frozen=True, slots=True)
class CameraHealthRow:
    camera_id: UUID
    tenant_id: UUID
    lot_id: UUID
    heartbeat_interval_sec: int
    status: CameraStatus
    last_heartbeat_at: datetime | None
    last_event_at: datetime | None


class CameraHealthStore(Protocol):
    async def active_cameras(self) -> list[CameraHealthRow]:
        """Кросс-tenant чтение ролью app_maintenance — собственная короткая транзакция."""
        ...


class CameraHealthWriter(Protocol):
    async def flush(
        self,
        uow: UnitOfWork,
        *,
        heartbeats: Sequence[tuple[UUID, datetime]],
        events: Sequence[tuple[UUID, datetime]],
        key_usage: Sequence[tuple[UUID, datetime]],
    ) -> None:
        """Батч-флаш ролью app_bulk в tenant-контексте UoW."""
        ...

    async def update_status(self, uow: UnitOfWork, camera_id: UUID, status: CameraStatus) -> None: ...


class EndpointResolver(Protocol):
    async def resolve(self, host: str) -> list[IPAddress]: ...


class CredentialBox(Protocol):
    """Шифрование учётных данных контроллера ключом клиента (platform.secret_box.TenantSecretBox)."""

    def seal(self, tenant_id: UUID, purpose: str, secret: bytes) -> bytes: ...
    def open(self, tenant_id: UUID, purpose: str, blob: bytes) -> bytes: ...
