"""Контракт Parking для ANPR Ingest (фаза 4) и API: команды ENTRY/EXIT и представления."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from parking.modules.access.application.contracts import AccessDecision
from parking.modules.parking.domain.saga import ExitSaga, SagaState
from parking.modules.parking.domain.session import ExitKind, ParkingSession, ReviewReason, SessionStatus
from parking.platform.db.uow import UnitOfWork
from parking.shared.plate import PlateNumber

__all__ = [
    "EntryCommand",
    "EntryResult",
    "EntryStatus",
    "ExitCommand",
    "ExitQuote",
    "ExitResult",
    "ExitStatus",
    "ParkingCommands",
    "SagaState",
    "SagaView",
    "SessionStatus",
    "SessionView",
]


class EntryStatus(enum.StrEnum):
    ENTRY_CONFIRMED = "ENTRY_CONFIRMED"
    ENTRY_DENIED = "ENTRY_DENIED"
    ENTRY_DENIED_FULL = "ENTRY_DENIED_FULL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    LATE_ACCEPTED = "LATE_ACCEPTED"
    ENTRY_EXIT_ECHO = "ENTRY_EXIT_ECHO"  # камера въезда увидела выезжающую машину — ни сессии, ни стрелы


class ExitStatus(enum.StrEnum):
    EXIT_CONFIRMED = "EXIT_CONFIRMED"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    EXIT_IN_PROGRESS = "EXIT_IN_PROGRESS"
    EXIT_WITHOUT_SESSION = "EXIT_WITHOUT_SESSION"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    LATE_ACCEPTED = "LATE_ACCEPTED"
    EXIT_ENTRY_ECHO = "EXIT_ENTRY_ECHO"  # камера выезда увидела въезжающую машину — ни выезда, ни стрелы


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryCommand:
    tenant_id: UUID
    lot_id: UUID
    plate: PlateNumber
    event_time: datetime
    decision: AccessDecision
    camera_id: UUID | None = None
    event_id: UUID | None = None
    confidence: int = 100
    requires_review: bool = False  # confidence в review-диапазоне
    late: bool = False  # буферизованное/опоздавшее событие: сессия задним числом, шлагбаум не трогать


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryResult:
    status: EntryStatus
    session_id: UUID | None
    barrier_command_id: UUID | None = None
    duplicate: bool = False
    orphaned_session_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitCommand:
    tenant_id: UUID
    lot_id: UUID
    plate: PlateNumber
    event_time: datetime
    event_id: UUID
    decision: AccessDecision
    camera_id: UUID | None = None
    confidence: int = 100
    requires_review: bool = False
    late: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitResult:
    status: ExitStatus
    session_id: UUID | None
    saga_id: UUID | None = None
    amount_minor: int = 0
    saga_state: SagaState | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitQuote:
    """Сколько должна сессия на момент `at` — считает Parking (тариф + решение доступа), кассир только видит."""

    session_id: UUID
    lot_id: UUID
    entry_time: datetime
    at: datetime
    amount_minor: int
    snapshot: dict[str, Any]
    access_kind: str
    source_id: UUID | None
    exit_grace_minutes: int


class ParkingCommands(Protocol):
    async def process_entry(self, cmd: EntryCommand) -> EntryResult:
        """Одна транзакция: vehicle upsert + сессия + команда шлагбауму + outbox (I-1 уникальным индексом)."""
        ...

    async def process_entry_in(self, uow: UnitOfWork, cmd: EntryCommand) -> EntryResult:
        """То же внутри транзакции вызывающего (ANPR ingest: идемпотентность + событие + сессия атомарно)."""
        ...

    async def start_exit(self, cmd: ExitCommand) -> ExitResult:
        """Одна транзакция: поиск сессии, расчёт, создание саги, outbox. Шаги саги — отдельными транзакциями."""
        ...

    async def start_exit_in(self, uow: UnitOfWork, cmd: ExitCommand) -> ExitResult: ...

    async def prepay_quote(self, tenant_id: UUID, session_id: UUID, at: datetime) -> ExitQuote:
        """Предоплата у кассира (A-11, SD-2b): сумма к оплате для ACTIVE-сессии без открытой саги."""
        ...

    async def close_inferred_by_payment(
        self, tenant_id: UUID, session_id: UUID, *, payment_id: UUID, paid_at: datetime
    ) -> bool:
        """Оплатили, но выехали через полосу без камеры: сессия закрывается по платежу, exit_time = paid_at,
        exit_kind INFERRED_BY_PAYMENT. Идемпотентно; False — сессия уже не ACTIVE или у неё открыта сага."""
        ...


@dataclass(frozen=True, slots=True)
class StaleSessionsResult:
    """Массовое закрытие старых сессий площадки: сколько нашлось, закрыто и пропущено (идёт выезд или оплата)."""

    older_than: datetime
    found: int
    closed: int
    skipped_in_exit: int


@dataclass(frozen=True, slots=True)
class SessionView:
    id: UUID
    lot_id: UUID
    vehicle_id: UUID
    status: SessionStatus
    entry_time: datetime
    exit_time: datetime | None
    billing_minutes: int | None
    exit_kind: ExitKind | None
    entry_access_kind: str
    entry_unknown: bool
    entry_inferred: bool
    review_reason: ReviewReason | None
    closed_reason: str | None
    entry_camera_id: UUID | None
    exit_camera_id: UUID | None
    tariff_plan_version_id: UUID | None
    version: int
    created_at: datetime | None
    plate: str | None = None  # маскированный, если запрошен
    entry_manual: bool = False
    vehicle_make: str | None = None
    vehicle_model: str | None = None
    vehicle_color: str | None = None

    @classmethod
    def of(
        cls, s: ParkingSession, plate: str | None = None, vehicle: VehicleAttributesView | None = None
    ) -> SessionView:
        return cls(
            id=s.id,
            lot_id=s.lot_id,
            vehicle_id=s.vehicle_id,
            status=s.status,
            entry_time=s.entry_time,
            exit_time=s.exit_time,
            billing_minutes=s.billing_minutes,
            exit_kind=s.exit_kind,
            entry_access_kind=s.entry_access_kind,
            entry_unknown=s.entry_unknown,
            entry_inferred=s.entry_inferred,
            review_reason=s.review_reason,
            closed_reason=s.closed_reason,
            entry_camera_id=s.entry_camera_id,
            exit_camera_id=s.exit_camera_id,
            tariff_plan_version_id=s.tariff_plan_version_id,
            version=s.version,
            created_at=s.created_at,
            plate=plate,
            entry_manual=s.entry_manual,
            vehicle_make=vehicle.make if vehicle else None,
            vehicle_model=vehicle.model if vehicle else None,
            vehicle_color=vehicle.color if vehicle else None,
        )


@dataclass(frozen=True, slots=True)
class SimilarSession:
    """Машина на площадке с похожим номером — кандидат для «выезда без въезда» (distance — взвешенная правка)."""

    session: SessionView
    distance: float


@dataclass(frozen=True, slots=True)
class VehicleAttributesView:
    """v2: марка/модель/цвет машины — вводятся оператором, не распознаются камерой."""

    make: str | None
    model: str | None
    color: str | None


@dataclass(frozen=True, slots=True)
class VehicleView:
    plate: str  # маскированный
    plate_format: str
    known: bool  # номер уже встречался (есть запись vehicle)
    attributes: VehicleAttributesView
    active_session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ManualEntryCommand:
    lot_id: UUID
    plate: str
    entry_time: datetime | None = None
    make: str | None = None
    model: str | None = None
    color: str | None = None


@dataclass(frozen=True, slots=True)
class SagaView:
    id: UUID
    session_id: UUID
    lot_id: UUID
    state: SagaState
    amount_due: int
    payment_id: UUID | None
    barrier_command_id: UUID | None
    barrier_result: str | None
    review_reason: str | None
    last_error: str | None
    step_deadline: datetime | None
    calculation: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, s: ExitSaga) -> SagaView:
        return cls(
            id=s.id,
            session_id=s.session_id,
            lot_id=s.lot_id,
            state=s.state,
            amount_due=s.amount_due,
            payment_id=s.payment_id,
            barrier_command_id=s.barrier_command_id,
            barrier_result=s.barrier_result,
            review_reason=s.review_reason,
            last_error=s.last_error,
            step_deadline=s.step_deadline,
            calculation={
                **s.calculation,
                **({"manual_command_id": s.flags["manual_command_id"]} if s.flags.get("manual_command_id") else {}),
            },
        )
