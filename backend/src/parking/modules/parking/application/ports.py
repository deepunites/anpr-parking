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

    async def cards(self, vehicle_ids: list[UUID]) -> dict[UUID, VehicleCard]:
        """То же для многих машин одним запросом (похожие номера среди машин на площадке)."""
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

    async def recent_entries(
        self, lot_id: UUID, *, since: datetime, until: datetime
    ) -> list[tuple[ParkingSession, bytes, int]]:
        """Сессии площадки, въехавшие камерой в [since, until] и ещё стоящие, с шифртекстом номера — одним запросом
        (эхо и двойное чтение на горячем пути въезда и выезда)."""
        ...

    async def recent_visits(self, lot_id: UUID, vehicle_id: UUID, *, since: datetime, before: datetime) -> int:
        """Сколько раз машина побывала на площадке с въездом в [since, before): закрытые сессии, стоявшие дольше
        минуты (эхо въезда и ошибки камер не считаются)."""
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

    async def active_lots(self) -> list[tuple[UUID, UUID]]:
        """(tenant_id, lot_id) действующих площадок всех клиентов — для обслуживания (сессии, счётчик занятости)."""
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


# ---------------------------------------------------------------- партнёры площадки (клуб)
@dataclass(frozen=True, slots=True)
class Partner:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    name: str
    price_per_car: int  # тийины за каждую отмеченную машину (обычно 0)
    active: bool = True
    monthly_fee: int = 0  # тийины в месяц — фиксированная плата партнёра (MediaPark: клуб платит в месяц)


@dataclass(frozen=True, slots=True)
class PartnerMember:
    user_id: UUID
    display_name: str
    login: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    id: UUID
    partner_id: UUID
    session_id: UUID
    cancelled_at: datetime | None


@dataclass(frozen=True, slots=True)
class ValidationRow:
    id: UUID
    session_id: UUID
    plate_encrypted: bytes
    enc_key_version: int
    entry_time: datetime
    exit_time: datetime | None
    session_status: str
    price_per_car: int
    created_at: datetime
    created_by: str | None
    cancelled_at: datetime | None
    cancel_reason: str | None


class PartnerRepository(Protocol):
    async def all(self) -> list[Partner]: ...
    async def get(self, partner_id: UUID, *, for_update: bool = False) -> Partner | None: ...
    async def add(self, partner: Partner) -> None: ...
    async def save(self, partner: Partner) -> None: ...
    async def of_user(self, user_id: UUID) -> Partner | None: ...
    async def add_member(self, tenant_id: UUID, partner_id: UUID, user_id: UUID) -> None: ...
    async def members(self, partner_id: UUID) -> list[PartnerMember]: ...
    async def add_validation(
        self, *, validation_id: UUID, partner: Partner, session_id: UUID, created_by: UUID
    ) -> None: ...
    async def get_validation(self, validation_id: UUID, *, for_update: bool = False) -> ValidationRecord | None: ...
    async def cancel_validation(self, validation_id: UUID, *, by: UUID, reason: str, at: datetime) -> None: ...
    async def validations(self, partner_id: UUID, *, since: datetime, until: datetime) -> list[ValidationRow]: ...
