"""Сессия стоянки: состояния и переходы (ARCHITECTURE §4.1). Каждый переход — одна транзакция application-слоя."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from parking.shared.errors import ConflictError, ValidationError
from parking.shared.events import DomainEvent


class SessionStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ORPHAN = "ORPHAN"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    CANCELLED = "CANCELLED"


class ExitKind(enum.StrEnum):
    CAMERA = "CAMERA"
    INFERRED_BY_PAYMENT = "INFERRED_BY_PAYMENT"
    MANUAL = "MANUAL"


class ReviewReason(enum.StrEnum):
    ENTRY_LOW_CONFIDENCE = "ENTRY_LOW_CONFIDENCE"
    ENTRY_BLACKLIST = "ENTRY_BLACKLIST"
    EXIT_LOW_CONFIDENCE = "EXIT_LOW_CONFIDENCE"
    EXIT_UNKNOWN_SESSION = "EXIT_UNKNOWN_SESSION"
    EXIT_BARRIER_FAILED = "EXIT_BARRIER_FAILED"
    EXIT_PAYMENT_UNVERIFIED = "EXIT_PAYMENT_UNVERIFIED"
    EXIT_BLACKLIST = "EXIT_BLACKLIST"

    @property
    def is_exit_stage(self) -> bool:
        return self.name.startswith("EXIT_")


TERMINAL = frozenset({SessionStatus.CLOSED, SessionStatus.ORPHAN, SessionStatus.CANCELLED})


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCreated(DomainEvent):
    session_id: UUID
    status: SessionStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionClosed(DomainEvent):
    session_id: UUID
    exit_kind: ExitKind


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionOrphaned(DomainEvent):
    session_id: UUID
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionMovedToReview(DomainEvent):
    session_id: UUID
    reason: ReviewReason


@dataclass(frozen=True, slots=True, kw_only=True)
class PlateCorrected(DomainEvent):
    session_id: UUID
    old_vehicle_id: UUID
    new_vehicle_id: UUID


@dataclass(slots=True)
class ParkingSession:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    vehicle_id: UUID
    entry_time: datetime
    status: SessionStatus = SessionStatus.ACTIVE
    entry_camera_id: UUID | None = None
    entry_event_id: UUID | None = None
    exit_camera_id: UUID | None = None
    exit_event_id: UUID | None = None
    exit_time: datetime | None = None
    billing_minutes: int | None = None
    tariff_plan_version_id: UUID | None = None
    exit_kind: ExitKind | None = None
    entry_access_kind: str = "NORMAL"
    entry_unknown: bool = False
    entry_inferred: bool = False
    entry_manual: bool = False  # v2: въезд зарегистрирован оператором вручную
    review_reason: ReviewReason | None = None
    closed_reason: str | None = None
    version: int = 1
    created_at: datetime | None = None
    events: list[DomainEvent] = field(default_factory=list)

    # --- запросы ---------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def occupies_space(self) -> bool:
        """Считается ли машина находящейся на площадке (ADR-05)."""
        if self.status is SessionStatus.ACTIVE:
            return True
        return (
            self.status is SessionStatus.MANUAL_REVIEW
            and self.review_reason is not None
            and self.review_reason.is_exit_stage
        )

    def _require_open(self) -> None:
        if self.is_terminal:
            raise ConflictError(f"session {self.id} is {self.status.value}")

    # --- переходы ---------------------------------------------------------
    def close(
        self,
        *,
        exit_time: datetime,
        billing_minutes: int,
        exit_kind: ExitKind,
        tariff_plan_version_id: UUID | None,
        exit_camera_id: UUID | None = None,
        exit_event_id: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        self._require_open()
        if exit_time < self.entry_time:
            raise ValidationError("exit_time is before entry_time")
        if billing_minutes < 0:
            raise ValidationError("billing_minutes must be non-negative")
        self.status = SessionStatus.CLOSED
        self.exit_time = exit_time
        self.billing_minutes = billing_minutes
        self.exit_kind = exit_kind
        self.tariff_plan_version_id = tariff_plan_version_id
        self.exit_camera_id = exit_camera_id
        self.exit_event_id = exit_event_id
        self.closed_reason = reason
        self.review_reason = None
        self.version += 1
        self.events.append(SessionClosed(session_id=self.id, exit_kind=exit_kind))

    def mark_orphan(self, reason: str) -> None:
        self._require_open()
        self.status = SessionStatus.ORPHAN
        self.closed_reason = reason
        self.version += 1
        self.events.append(SessionOrphaned(session_id=self.id, reason=reason))

    def move_to_review(self, reason: ReviewReason) -> None:
        self._require_open()
        self.status = SessionStatus.MANUAL_REVIEW
        self.review_reason = reason
        self.version += 1
        self.events.append(SessionMovedToReview(session_id=self.id, reason=reason))

    def confirm_entry(self) -> None:
        """Оператор подтвердил распознавание/пропуск на въезде: MANUAL_REVIEW (entry) → ACTIVE."""
        if (
            self.status is not SessionStatus.MANUAL_REVIEW
            or self.review_reason is None
            or self.review_reason.is_exit_stage
        ):
            raise ConflictError("session is not awaiting entry confirmation")
        self.status = SessionStatus.ACTIVE
        self.review_reason = None
        self.version += 1

    def cancel(self, reason: str) -> None:
        if self.status is not SessionStatus.MANUAL_REVIEW:
            raise ConflictError("only sessions under review can be cancelled")
        self.status = SessionStatus.CANCELLED
        self.closed_reason = reason
        self.version += 1

    def correct_plate(self, new_vehicle_id: UUID) -> UUID:
        """Меняет привязку к машине; исходное распознавание остаётся в anpr_event/manual_correction (I-16)."""
        self._require_open()
        if new_vehicle_id == self.vehicle_id:
            raise ValidationError("plate is unchanged")
        old = self.vehicle_id
        self.vehicle_id = new_vehicle_id
        self.version += 1
        self.events.append(PlateCorrected(session_id=self.id, old_vehicle_id=old, new_vehicle_id=new_vehicle_id))
        return old

    def pull_events(self) -> list[DomainEvent]:
        events, self.events = self.events, []
        return events

    @staticmethod
    def minutes_between(entry: datetime, exit: datetime) -> int:
        return max(0, math.ceil((exit - entry).total_seconds() / 60))
