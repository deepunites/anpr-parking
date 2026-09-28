"""Запрос субъекта ПДн на удаление (ЗРУ-547 / GDPR как надмножество), фаза 10 §3.

Цикл: REQUESTED → IDENTITY_VERIFIED → LEGAL_RETENTION_CHECK → ANONYMIZATION → MEDIA_DELETION → AUDIT → COMPLETED
(REJECTED — из любого нетерминального состояния, с причиной). Финансовые строки (сессии, платежи) не удаляются —
номер заменяется необратимым токеном, суммы и даты остаются (ретеншен 7 лет). Срок ответа — SLA_DAYS (A-67).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from parking.shared.errors import ConflictError, ValidationError

SLA_DAYS = 10  # ЗРУ-547: ответ субъекту в течение десяти дней; GDPR (30 дней) — надмножество (A-67)


class DeletionState(enum.StrEnum):
    REQUESTED = "REQUESTED"
    IDENTITY_VERIFIED = "IDENTITY_VERIFIED"
    LEGAL_RETENTION_CHECK = "LEGAL_RETENTION_CHECK"
    ANONYMIZATION = "ANONYMIZATION"
    MEDIA_DELETION = "MEDIA_DELETION"
    AUDIT = "AUDIT"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


_ORDER = [
    DeletionState.REQUESTED,
    DeletionState.IDENTITY_VERIFIED,
    DeletionState.LEGAL_RETENTION_CHECK,
    DeletionState.ANONYMIZATION,
    DeletionState.MEDIA_DELETION,
    DeletionState.AUDIT,
    DeletionState.COMPLETED,
]
TERMINAL = frozenset({DeletionState.COMPLETED, DeletionState.REJECTED})
LEGAL_BASES = frozenset({"SUBJECT_REQUEST", "CONSENT_WITHDRAWN", "RETENTION_EXPIRED", "COURT_ORDER"})


@dataclass(slots=True, kw_only=True)
class DeletionRequest:
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID
    state: DeletionState = DeletionState.REQUESTED
    subject_plate_hash: bytes
    subject_contact_encrypted: bytes | None = None
    legal_basis: str
    requested_at: datetime
    due_at: datetime
    verified_at: datetime | None = None
    verified_by: UUID | None = None
    retention_note: str | None = None
    anonymized_at: datetime | None = None
    media_deleted_at: datetime | None = None
    audited_at: datetime | None = None
    completed_at: datetime | None = None
    rejected_reason: str | None = None
    created_by: UUID | None = None
    vehicle_ids: list[UUID] = field(default_factory=list)  # машины субъекта, найденные при проверке ретеншена

    @classmethod
    def open(
        cls,
        *,
        tenant_id: UUID,
        subject_plate_hash: bytes,
        legal_basis: str,
        now: datetime,
        contact_encrypted: bytes | None = None,
        created_by: UUID | None = None,
    ) -> DeletionRequest:
        if legal_basis not in LEGAL_BASES:
            raise ValidationError(f"legal_basis must be one of {sorted(LEGAL_BASES)}")
        return cls(
            tenant_id=tenant_id,
            subject_plate_hash=subject_plate_hash,
            subject_contact_encrypted=contact_encrypted,
            legal_basis=legal_basis,
            requested_at=now,
            due_at=now + timedelta(days=SLA_DAYS),
            created_by=created_by,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def is_overdue(self, now: datetime) -> bool:
        return not self.is_terminal and now > self.due_at

    def _advance(self, expected: DeletionState, now: datetime) -> None:
        if self.state is not expected:
            raise ConflictError(f"deletion request {self.id} is {self.state.value}, expected {expected.value}")
        self.state = _ORDER[_ORDER.index(expected) + 1]

    def verify_identity(self, by: UUID, now: datetime) -> None:
        self._advance(DeletionState.REQUESTED, now)
        self.verified_at, self.verified_by = now, by

    def retention_checked(self, note: str, now: datetime) -> None:
        self._advance(DeletionState.IDENTITY_VERIFIED, now)
        self.retention_note = note

    def anonymized(self, now: datetime) -> None:
        self._advance(DeletionState.LEGAL_RETENTION_CHECK, now)
        self.anonymized_at = now

    def media_deleted(self, now: datetime) -> None:
        self._advance(DeletionState.ANONYMIZATION, now)
        self.media_deleted_at = now

    def audited(self, now: datetime) -> None:
        self._advance(DeletionState.MEDIA_DELETION, now)
        self.audited_at = now

    def complete(self, now: datetime) -> None:
        self._advance(DeletionState.AUDIT, now)
        self.completed_at = now

    def reject(self, reason: str, now: datetime) -> None:
        if self.is_terminal:
            raise ConflictError(f"deletion request {self.id} is {self.state.value}")
        if len(reason.strip()) < 3:
            raise ValidationError("rejection reason is required")
        self.state = DeletionState.REJECTED
        self.rejected_reason = reason
        self.completed_at = now
