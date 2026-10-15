from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from parking.modules.parking.domain.saga import ExitSaga
from parking.modules.parking.domain.session import ParkingSession, SessionStatus


@dataclass(frozen=True, slots=True)
class VehicleRef:
    id: UUID
    plate_hash: bytes


@dataclass(frozen=True, slots=True)
class VehicleAttributes:
    make: str | None
    model: str | None
    color: str | None


@dataclass(frozen=True, slots=True)
class VehicleCard:
    plate_encrypted: bytes
    enc_key_version: int
    attributes: VehicleAttributes


class VehicleRepository(Protocol):
    async def upsert(
        self, plate_hash: bytes, plate_encrypted: bytes, enc_key_version: int, seen_at: datetime
    ) -> VehicleRef:
        """INSERT … ON CONFLICT (tenant_id, plate_hash) DO UPDATE last_seen_at RETURNING id."""
        ...

    async def plate_encrypted(self, vehicle_id: UUID) -> tuple[bytes, int] | None: ...

    async def card(self, vehicle_id: UUID) -> VehicleCard | None:
        """Шифртекст номера + марка/модель/цвет одним запросом (карточка сессии, списки)."""
        ...

    async def set_attributes(
        self, vehicle_id: UUID, *, make: str | None, model: str | None, color: str | None, at: datetime
    ) -> None: ...

    async def attributes_by_hashes(self, plate_hashes: list[bytes]) -> dict[bytes, VehicleAttributes]: ...


class SessionRepository(Protocol):
    async def add(self, session: ParkingSession) -> None:
        """Бросает ConflictError при нарушении I-1 (вторая ACTIVE на (lot, vehicle)).

        Транзакция остаётся живой: конфликт гасится арбитром индекса (ON CONFLICT DO NOTHING), не ошибкой.
        """
        ...

    async def get(self, session_id: UUID) -> ParkingSession | None: ...
    async def get_for_update(self, session_id: UUID) -> ParkingSession | None: ...
    async def save(self, session: ParkingSession) -> None:
        """UPDATE … WHERE version = ожидаемая − 1; расхождение → ConflictError (оптимистичная блокировка)."""
        ...

    async def active_for_vehicle(
        self, lot_id: UUID, vehicle_id: UUID, *, for_update: bool = False
    ) -> ParkingSession | None: ...
    async def open_for_plate(
        self, lot_id: UUID, plate_hashes: list[bytes], *, for_update: bool = False
    ) -> ParkingSession | None:
        """Сессия, занимающая место (ACTIVE или MANUAL_REVIEW стадии выезда), по хешам номера (join vehicle)."""
        ...

    async def list_by_lot(
        self,
        lot_id: UUID,
        *,
        statuses: tuple[SessionStatus, ...] | None,
        limit: int,
        after: tuple[datetime, UUID] | None,
    ) -> list[ParkingSession]: ...
    async def list_by_plate_hashes(self, plate_hashes: list[bytes], *, limit: int) -> list[ParkingSession]: ...
    async def review_queue(self, lot_ids: frozenset[UUID] | None, *, limit: int) -> list[ParkingSession]: ...
    async def count_occupying(self, lot_id: UUID) -> int: ...
    async def count_occupying_by_entry_camera(self, lot_id: UUID) -> dict[UUID | None, int]: ...
    async def stale_active(self, older_than: datetime, *, limit: int) -> list[ParkingSession]: ...
    async def exiting_session(
        self, lot_id: UUID, vehicle_id: UUID, *, exited_since: datetime, saga_since: datetime
    ) -> UUID | None:
        """Сессия машины, которая сейчас выезжает (сага выезда идёт с saga_since) или выехала с exited_since —
        в том числе «выезд без въезда» на разбор, созданный с exited_since."""
        ...

    async def open_in_lot_before(self, lot_id: UUID, older_than: datetime, *, limit: int) -> list[ParkingSession]:
        """Незакрытые сессии площадки (ACTIVE, MANUAL_REVIEW) с въездом раньше older_than — от старых к новым."""
        ...


class SagaRepository(Protocol):
    async def add(self, saga: ExitSaga) -> None: ...
    async def get(self, saga_id: UUID) -> ExitSaga | None: ...
    async def get_for_update(self, saga_id: UUID) -> ExitSaga | None: ...
    async def active_for_session(self, session_id: UUID, *, for_update: bool = False) -> ExitSaga | None: ...
    async def save(self, saga: ExitSaga) -> None: ...
    async def review_queue(self, lot_ids: frozenset[UUID] | None, *, limit: int) -> list[ExitSaga]: ...


@dataclass(frozen=True, slots=True)
class DueSaga:
    tenant_id: UUID
    saga_id: UUID


class SagaScanner(Protocol):
    async def due(self, now: datetime, *, limit: int) -> list[DueSaga]:
        """Кросс-tenant выборка просроченных шагов (роль app_maintenance) — восстановление после падений."""
        ...


class CorrectionRepository(Protocol):
    async def add(
        self,
        *,
        correction_id: UUID,
        session: ParkingSession,
        event_id: UUID | None,
        old_vehicle_id: UUID,
        new_vehicle_id: UUID,
        plate_encrypted: bytes,
        enc_key_version: int,
        reason: str,
        user_id: UUID,
    ) -> None: ...


class OccupancyProjection(Protocol):
    """Счётчик в Redis (ADR-05). Все методы деградируют к None/пропуску при недоступности Redis."""

    async def get(self, lot_id: UUID) -> int | None: ...
    async def adjust(self, lot_id: UUID, delta: int) -> None: ...
    async def set(self, lot_id: UUID, value: int) -> None: ...
