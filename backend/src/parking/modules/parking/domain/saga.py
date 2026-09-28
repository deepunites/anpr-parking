"""EXIT-сага (ARCHITECTURE §4.4): персистентное состояние, идемпотентные шаги, таймауты, компенсации."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from parking.shared.errors import ConflictError


class SagaState(enum.StrEnum):
    ACCESS_RESOLVED = "ACCESS_RESOLVED"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    PAYMENT_PENDING_VERIFICATION = "PAYMENT_PENDING_VERIFICATION"
    PAYMENT_SETTLED = "PAYMENT_SETTLED"
    BARRIER_OPENING = "BARRIER_OPENING"
    SESSION_CLOSED = "SESSION_CLOSED"
    COMPLETED = "COMPLETED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({SagaState.COMPLETED, SagaState.EXPIRED, SagaState.CANCELLED})

STEP_TIMEOUTS: dict[SagaState, timedelta] = {
    SagaState.ACCESS_RESOLVED: timedelta(seconds=5),
    SagaState.PAYMENT_REQUIRED: timedelta(minutes=15),
    SagaState.PAYMENT_PENDING_VERIFICATION: timedelta(minutes=5),
    SagaState.PAYMENT_SETTLED: timedelta(seconds=5),
    SagaState.BARRIER_OPENING: timedelta(seconds=30),
    SagaState.SESSION_CLOSED: timedelta(seconds=5),
    SagaState.MANUAL_REVIEW: timedelta(days=365),
}
SAGA_TIMEOUT = timedelta(minutes=30)
RECOVERY_GRACE = timedelta(seconds=10)  # runner подбирает саги, чей дедлайн шага прошёл: «kill -9» между шагами


@dataclass(slots=True)
class ExitSaga:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    session_id: UUID
    exit_event_id: UUID
    exit_event_time: datetime
    access_decision: dict[str, Any]
    calculation: dict[str, Any]
    amount_due: int
    correlation_id: UUID
    created_at: datetime
    state: SagaState = SagaState.ACCESS_RESOLVED
    step_deadline: datetime | None = None
    saga_deadline: datetime | None = None
    exit_camera_id: UUID | None = None
    additional_event_ids: list[UUID] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    payment_id: UUID | None = None
    barrier_command_id: UUID | None = None
    barrier_result: str | None = None
    attempts: int = 0
    last_error: str | None = None
    review_reason: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.step_deadline is None:
            self.step_deadline = self.created_at + STEP_TIMEOUTS[self.state]
        if self.saga_deadline is None:
            self.saga_deadline = self.created_at + SAGA_TIMEOUT

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def skip_barrier(self) -> bool:
        return bool(self.flags.get("late"))

    def _go(self, state: SagaState, now: datetime) -> None:
        if self.is_terminal:
            raise ConflictError(f"saga {self.id} is {self.state.value}")
        self.state = state
        self.step_deadline = now + STEP_TIMEOUTS.get(state, timedelta(0))
        self.attempts = 0
        self.last_error = None

    def require_payment(self, payment_id: UUID, now: datetime) -> None:
        self.payment_id = payment_id
        self._go(SagaState.PAYMENT_REQUIRED, now)

    def payment_unverified(self, now: datetime) -> None:
        self._go(SagaState.PAYMENT_PENDING_VERIFICATION, now)

    def settle(self, payment_id: UUID | None, now: datetime) -> None:
        if payment_id is not None:
            self.payment_id = payment_id
        self._go(SagaState.PAYMENT_SETTLED, now)

    def open_barrier(self, command_id: UUID | None, now: datetime) -> None:
        self.barrier_command_id = command_id
        self._go(SagaState.BARRIER_OPENING, now)

    def barrier_done(self, result: str, now: datetime) -> None:
        self.barrier_result = result
        self._go(SagaState.SESSION_CLOSED, now)

    def complete(self, now: datetime) -> None:
        self._go(SagaState.COMPLETED, now)

    def manual_review(self, reason: str, now: datetime, error: str | None = None) -> None:
        self._go(SagaState.MANUAL_REVIEW, now)
        self.review_reason = reason
        self.last_error = error

    def expire(self, now: datetime) -> None:
        self._go(SagaState.EXPIRED, now)

    def cancel(self, reason: str, now: datetime) -> None:
        self._go(SagaState.CANCELLED, now)
        self.review_reason = reason

    def attach_event(self, event_id: UUID) -> None:
        if event_id not in self.additional_event_ids and event_id != self.exit_event_id:
            self.additional_event_ids.append(event_id)

    def is_due(self, now: datetime) -> bool:
        return not self.is_terminal and self.step_deadline is not None and self.step_deadline + RECOVERY_GRACE <= now
