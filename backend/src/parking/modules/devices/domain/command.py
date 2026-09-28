"""Команда шлагбауму: записывается до отправки, результат — после (I-9). Ручная — только с issued_by и reason (I-15)."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from parking.shared.errors import ValidationError


class CommandKind(enum.StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    STATUS = "STATUS"


class CommandState(enum.StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    OK = "OK"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CommandSource(enum.StrEnum):
    AUTO_ENTRY = "AUTO_ENTRY"
    AUTO_EXIT = "AUTO_EXIT"
    MANUAL = "MANUAL"
    SYSTEM = "SYSTEM"


@dataclass(slots=True)
class BarrierCommand:
    id: UUID
    tenant_id: UUID
    lot_id: UUID
    barrier_id: UUID
    kind: CommandKind
    source: CommandSource
    idempotency_key: bytes
    correlation_id: UUID
    created_at: datetime
    state: CommandState = CommandState.PENDING
    session_id: UUID | None = None
    saga_id: UUID | None = None
    issued_by: UUID | None = None
    reason: str | None = None
    attempts: int = 0
    last_error: str | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        if self.source is CommandSource.MANUAL and (self.issued_by is None or not (self.reason or "").strip()):
            raise ValidationError("manual barrier command requires issued_by and reason (I-15)")

    @property
    def is_terminal(self) -> bool:
        return self.state in (CommandState.OK, CommandState.FAILED, CommandState.SKIPPED)

    def mark_sent(self) -> None:
        if self.state is not CommandState.PENDING:
            raise ValidationError(f"command {self.id} is {self.state.value}, cannot send")
        self.state = CommandState.SENT
        self.attempts += 1

    def complete(self, *, ok: bool, error: str | None, latency_ms: int | None) -> None:
        if self.state is not CommandState.SENT:
            raise ValidationError(f"command {self.id} is {self.state.value}, cannot complete")
        self.state = CommandState.OK if ok else CommandState.FAILED
        self.last_error = error
        self.latency_ms = latency_ms
